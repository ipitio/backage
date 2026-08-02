"""Bounded worker execution for Python-owned bkg pipelines."""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from math import ceil
from typing import TYPE_CHECKING, Literal

from .runtime import GracefulStop

if TYPE_CHECKING:
    from .config import RuntimeConfig

_DEFAULT_STOP_GRACE_SECONDS = 180.0
_DEFAULT_POLL_INTERVAL_SECONDS = 0.1

WorkerEventKind = Literal[
    "submitted",
    "completed",
    "failed",
    "stop-requested",
    "cancelled",
    "drain-timeout",
]
TaskInterruptionReason = Literal["cancelled", "drain-timeout"]
EventSink = Callable[["WorkerEvent"], None]


def _no_stop_check() -> None:
    """Default stop check for callers that do not have runtime control yet."""


@dataclass(frozen=True)
class ConcurrencySettings:
    """Python-side worker settings matching existing shell tuning inputs."""

    max_workers: int
    stop_grace_seconds: float = _DEFAULT_STOP_GRACE_SECONDS

    @classmethod
    def from_config(cls, config: RuntimeConfig) -> ConcurrencySettings:
        """Load worker settings captured by the application configuration."""

        return cls(
            max_workers=config.parallel_async_max_jobs,
            stop_grace_seconds=config.owner_update_stop_grace,
        )


@dataclass(frozen=True)
class WorkerEvent:
    """Structured progress event for one bounded worker task."""

    kind: WorkerEventKind
    index: int | None = None
    name: str = ""
    message: str = ""


@dataclass(frozen=True)
class TaskResult[R]:
    """Successful result from one bounded task."""

    index: int
    name: str
    value: R


@dataclass(frozen=True)
class TaskFailure:
    """Failure from one bounded task."""

    index: int
    name: str
    error: Exception


@dataclass(frozen=True)
class TaskInterruption:
    """Task that did not complete before a stop or failure drain ended."""

    index: int
    name: str
    reason: TaskInterruptionReason


@dataclass(frozen=True)
class WorkerRunCounts:
    """Task and worker counts for one bounded run."""

    requested_tasks: int
    submitted_tasks: int
    max_workers: int
    peak_in_flight: int


@dataclass(frozen=True)
class WorkerRunTiming:
    """Whole-run wall, process CPU, task, and drain timings."""

    wall_seconds: float
    process_cpu_seconds: float
    task_seconds: float
    drain_seconds: float


@dataclass(frozen=True)
class WorkerTaskMetrics:
    """Dispatch and task-duration distribution for one bounded run."""

    queue_wait_p95_seconds: float
    queue_wait_max_seconds: float
    task_p50_seconds: float
    task_p95_seconds: float
    task_max_seconds: float
    slowest_task: str


@dataclass(frozen=True)
class WorkerRunMetrics:
    """Aggregate measurements for one bounded worker run."""

    counts: WorkerRunCounts
    timing: WorkerRunTiming
    tasks: WorkerTaskMetrics

    @property
    def worker_occupancy(self) -> float:
        """Return the fraction of available worker time spent inside tasks."""

        capacity = self.timing.wall_seconds * self.counts.max_workers
        if capacity <= 0:
            return 0.0
        return min(1.0, self.timing.task_seconds / capacity)

    @property
    def process_cpu_cores(self) -> float:
        """Return process CPU time as an equivalent average core count."""

        if self.timing.wall_seconds <= 0:
            return 0.0
        return self.timing.process_cpu_seconds / self.timing.wall_seconds


@dataclass(frozen=True)
class BoundedRunResult[R]:
    """Completed work and the first task failure, if any."""

    completed: tuple[TaskResult[R], ...]
    failure: TaskFailure | None = None
    failures: tuple[TaskFailure, ...] = ()
    interrupted: tuple[TaskInterruption, ...] = ()
    drain_timed_out: bool = False
    metrics: WorkerRunMetrics | None = None

    @property
    def stopped(self) -> bool:
        """Return whether graceful stop was observed during the run."""

        return any(isinstance(failure.error, GracefulStop) for failure in self.failures)

    @property
    def ok(self) -> bool:
        """Return whether every task completed successfully."""

        return not self.failures and not self.interrupted


@dataclass
class _TaskTiming:
    submitted_at: float
    started_at: float | None = None
    finished_at: float | None = None


@dataclass(frozen=True)
class _TaskTimingSample:
    name: str
    queue_wait_seconds: float
    task_seconds: float


@dataclass(frozen=True)
class _InFlight[T]:
    index: int
    name: str
    item: T
    timing: _TaskTiming


@dataclass
class _RunProgress:
    next_index: int = 0
    peak_in_flight: int = 0
    drain_started_at: float | None = None
    drain_timed_out: bool = False


@dataclass
class _RunState[T, R]:
    completed: list[TaskResult[R]]
    failures: list[TaskFailure]
    interrupted: list[TaskInterruption]
    futures: dict[Future[R], _InFlight[T]]
    timings: list[_TaskTimingSample]
    progress: _RunProgress


@dataclass(frozen=True)
class _RunPlan[T, R]:
    items: Sequence[T]
    worker: Callable[[T], R]
    task_name: Callable[[T], str]


@dataclass(frozen=True)
class _RunMeasurement:
    requested_tasks: int
    max_workers: int
    started_at: float
    finished_at: float
    process_cpu_seconds: float


@dataclass(frozen=True)
class BoundedWorkerRunner:
    """Run work with bounded threads and deterministic stop handling."""

    settings: ConcurrencySettings
    check_stop: Callable[[], None] = _no_stop_check
    event_sink: EventSink | None = None
    clock: Callable[[], float] = time.monotonic
    cpu_clock: Callable[[], float] = time.process_time
    poll_interval: float = _DEFAULT_POLL_INTERVAL_SECONDS

    def run[T, R](
        self,
        items: Sequence[T],
        worker: Callable[[T], R],
        *,
        task_name: Callable[[T], str] = str,
    ) -> BoundedRunResult[R]:
        """Run items with bounded concurrency and deterministic completion records."""

        self._validate()
        started_at = self.clock()
        cpu_started_at = self.cpu_clock()
        plan = _RunPlan(items, worker, task_name)
        state: _RunState[T, R] = _RunState([], [], [], {}, [], _RunProgress())
        self._execute(state, plan)
        finished_at = self.clock()
        metrics = _worker_run_metrics(
            state,
            _RunMeasurement(
                requested_tasks=len(items),
                max_workers=self.settings.max_workers,
                started_at=started_at,
                finished_at=finished_at,
                process_cpu_seconds=max(0.0, self.cpu_clock() - cpu_started_at),
            ),
        )
        return _finish_result(state, metrics)

    def _execute[T, R](
        self,
        state: _RunState[T, R],
        plan: _RunPlan[T, R],
    ) -> None:
        executor = ThreadPoolExecutor(max_workers=self.settings.max_workers)

        try:
            self._fill_workers(executor, state, plan)

            while state.futures:
                self._record_external_stop(state, plan)
                if not state.progress.drain_timed_out and self._drain_expired(state):
                    self._report_drain_timeout(state)

                done, _pending = wait(
                    state.futures,
                    timeout=self._wait_timeout(state),
                    return_when=FIRST_COMPLETED,
                )
                for future in done:
                    task = state.futures.pop(future)
                    self._collect_result(future, task, state)

                self._fill_workers(executor, state, plan)
        finally:
            executor.shutdown(wait=True, cancel_futures=True)

    def _validate(self) -> None:
        if self.settings.max_workers <= 0:
            raise ValueError("max_workers must be greater than zero")
        if self.settings.stop_grace_seconds < 0:
            raise ValueError("stop_grace_seconds must be zero or greater")
        if self.poll_interval <= 0:
            raise ValueError("poll_interval must be greater than zero")

    def _fill_workers[T, R](
        self,
        executor: ThreadPoolExecutor,
        state: _RunState[T, R],
        plan: _RunPlan[T, R],
    ) -> None:
        while (
            not state.failures
            and len(state.futures) < self.settings.max_workers
            and state.progress.next_index < len(plan.items)
        ):
            self._submit_next(executor, state, plan)

    def _submit_next[T, R](
        self,
        executor: ThreadPoolExecutor,
        state: _RunState[T, R],
        plan: _RunPlan[T, R],
    ) -> None:
        try:
            self.check_stop()
        except GracefulStop as error:
            self._record_failure(
                state,
                TaskFailure(
                    state.progress.next_index,
                    _pending_task_name(
                        plan.items,
                        state.progress.next_index,
                        plan.task_name,
                    ),
                    error,
                ),
            )
            return

        item = plan.items[state.progress.next_index]
        name = plan.task_name(item)
        timing = _TaskTiming(self.clock())
        state.futures[
            executor.submit(_run_timed, plan.worker, item, timing, self.clock)
        ] = _InFlight(
            state.progress.next_index,
            name,
            item,
            timing,
        )
        state.progress.peak_in_flight = max(
            state.progress.peak_in_flight,
            len(state.futures),
        )
        self._emit("submitted", state.progress.next_index, name)
        state.progress.next_index += 1

    def _record_external_stop[T, R](
        self,
        state: _RunState[T, R],
        plan: _RunPlan[T, R],
    ) -> None:
        if state.failures:
            return
        try:
            self.check_stop()
        except GracefulStop as error:
            self._record_failure(
                state,
                TaskFailure(
                    state.progress.next_index,
                    _pending_task_name(
                        plan.items,
                        state.progress.next_index,
                        plan.task_name,
                    ),
                    error,
                ),
            )

    def _collect_result[T, R](
        self,
        future: Future[R],
        task: _InFlight[T],
        state: _RunState[T, R],
    ) -> None:
        self._record_timing(task, state)
        try:
            result = future.result()
        except Exception as error:  # noqa: BLE001  # pylint: disable=broad-exception-caught
            self._record_failure(state, TaskFailure(task.index, task.name, error))
        else:
            state.completed.append(TaskResult(task.index, task.name, result))
            self._emit("completed", task.index, task.name)

    @staticmethod
    def _record_timing[T, R](
        task: _InFlight[T],
        state: _RunState[T, R],
    ) -> None:
        started_at = task.timing.started_at
        finished_at = task.timing.finished_at
        if started_at is None or finished_at is None:
            return
        state.timings.append(
            _TaskTimingSample(
                task.name,
                max(0.0, started_at - task.timing.submitted_at),
                max(0.0, finished_at - started_at),
            )
        )

    def _record_failure[T, R](
        self,
        state: _RunState[T, R],
        failure: TaskFailure,
    ) -> None:
        stop_was_reported = any(
            isinstance(record.error, GracefulStop) for record in state.failures
        )
        state.failures.append(failure)
        if state.progress.drain_started_at is None:
            state.progress.drain_started_at = self.clock()
        if isinstance(failure.error, GracefulStop):
            if not stop_was_reported:
                self._emit(
                    "stop-requested",
                    failure.index,
                    failure.name,
                    str(failure.error),
                )
        else:
            self._emit(
                "failed",
                failure.index,
                failure.name,
                str(failure.error),
            )

    def _drain_expired[T, R](self, state: _RunState[T, R]) -> bool:
        if not state.futures or state.progress.drain_started_at is None:
            return False
        return (
            self.clock() - state.progress.drain_started_at
        ) >= self.settings.stop_grace_seconds

    def _wait_timeout[T, R](self, state: _RunState[T, R]) -> float:
        if state.progress.drain_started_at is None or state.progress.drain_timed_out:
            return self.poll_interval
        remaining = self.settings.stop_grace_seconds - (
            self.clock() - state.progress.drain_started_at
        )
        return max(0.0, min(self.poll_interval, remaining))

    def _report_drain_timeout[T, R](self, state: _RunState[T, R]) -> None:
        """Report overdue workers without allowing them to outlive the runner."""

        state.progress.drain_timed_out = True
        for future, task in list(state.futures.items()):
            if future.done():
                state.futures.pop(future)
                self._collect_result(future, task, state)
                continue
            reason: TaskInterruptionReason
            event_kind: WorkerEventKind
            if future.cancel():
                reason = "cancelled"
                event_kind = "cancelled"
                state.futures.pop(future)
            else:
                reason = "drain-timeout"
                event_kind = "drain-timeout"
            state.interrupted.append(TaskInterruption(task.index, task.name, reason))
            self._emit(event_kind, task.index, task.name)

    def _emit(
        self,
        kind: WorkerEventKind,
        index: int | None = None,
        name: str = "",
        message: str = "",
    ) -> None:
        if self.event_sink is not None:
            self.event_sink(WorkerEvent(kind, index, name, message))


def run_bounded[T, R](
    items: Sequence[T],
    worker: Callable[[T], R],
    *,
    max_workers: int,
    check_stop: Callable[[], None] = _no_stop_check,
    task_name: Callable[[T], str] = str,
) -> BoundedRunResult[R]:
    """Run items with bounded concurrency and deterministic completion records."""

    return BoundedWorkerRunner(
        ConcurrencySettings(max_workers=max_workers),
        check_stop=check_stop,
    ).run(items, worker, task_name=task_name)


def _finish_result[T, R](
    state: _RunState[T, R],
    metrics: WorkerRunMetrics,
) -> BoundedRunResult[R]:
    state.completed.sort(key=lambda result: result.index)
    state.failures.sort(key=lambda failure: failure.index)
    state.interrupted.sort(key=lambda interruption: interruption.index)
    failures = tuple(state.failures)
    return BoundedRunResult(
        completed=tuple(state.completed),
        failure=failures[0] if failures else None,
        failures=failures,
        interrupted=tuple(state.interrupted),
        drain_timed_out=state.progress.drain_timed_out,
        metrics=metrics,
    )


def _run_timed[T, R](
    worker: Callable[[T], R],
    item: T,
    timing: _TaskTiming,
    clock: Callable[[], float],
) -> R:
    timing.started_at = clock()
    try:
        return worker(item)
    finally:
        timing.finished_at = clock()


def _worker_run_metrics[T, R](
    state: _RunState[T, R],
    measurement: _RunMeasurement,
) -> WorkerRunMetrics:
    task_seconds = sorted(sample.task_seconds for sample in state.timings)
    queue_wait_seconds = sorted(sample.queue_wait_seconds for sample in state.timings)
    slowest = max(state.timings, key=lambda sample: sample.task_seconds, default=None)
    drain_seconds = (
        max(0.0, measurement.finished_at - state.progress.drain_started_at)
        if state.progress.drain_started_at is not None
        else 0.0
    )
    return WorkerRunMetrics(
        counts=WorkerRunCounts(
            requested_tasks=measurement.requested_tasks,
            submitted_tasks=state.progress.next_index,
            max_workers=measurement.max_workers,
            peak_in_flight=state.progress.peak_in_flight,
        ),
        timing=WorkerRunTiming(
            wall_seconds=max(
                0.0,
                measurement.finished_at - measurement.started_at,
            ),
            process_cpu_seconds=measurement.process_cpu_seconds,
            task_seconds=sum(task_seconds),
            drain_seconds=drain_seconds,
        ),
        tasks=WorkerTaskMetrics(
            queue_wait_p95_seconds=_percentile(queue_wait_seconds, 0.95),
            queue_wait_max_seconds=max(queue_wait_seconds, default=0.0),
            task_p50_seconds=_percentile(task_seconds, 0.50),
            task_p95_seconds=_percentile(task_seconds, 0.95),
            task_max_seconds=max(task_seconds, default=0.0),
            slowest_task=slowest.name if slowest is not None else "",
        ),
    )


def _percentile(values: Sequence[float], percentile: float) -> float:
    """Return the nearest-rank percentile from ascending values."""

    if not values:
        return 0.0
    index = max(0, ceil(percentile * len(values)) - 1)
    return values[index]


def _pending_task_name[T](
    items: Sequence[T],
    index: int,
    task_name: Callable[[T], str],
) -> str:
    if index >= len(items):
        return ""
    return task_name(items[index])
