"""Bounded worker execution for Python-owned bkg pipelines."""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Literal

from .runtime import GracefulStop

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
    def from_env(cls) -> ConcurrencySettings:
        """Load bounded-worker settings from shell-compatible environment."""

        return cls(
            max_workers=_env_positive_int(
                "BKG_PARALLEL_ASYNC_MAX_JOBS",
                _default_max_workers(),
            ),
            stop_grace_seconds=_env_positive_float(
                "BKG_OWNER_UPDATE_STOP_GRACE",
                _DEFAULT_STOP_GRACE_SECONDS,
            ),
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
class BoundedRunResult[R]:
    """Completed work and the first task failure, if any."""

    completed: tuple[TaskResult[R], ...]
    failure: TaskFailure | None = None
    failures: tuple[TaskFailure, ...] = ()
    interrupted: tuple[TaskInterruption, ...] = ()
    drain_timed_out: bool = False

    @property
    def stopped(self) -> bool:
        """Return whether graceful stop was observed during the run."""

        return any(isinstance(failure.error, GracefulStop) for failure in self.failures)

    @property
    def ok(self) -> bool:
        """Return whether every task completed successfully."""

        return not self.failures and not self.interrupted


@dataclass(frozen=True)
class _InFlight[T]:
    index: int
    name: str
    item: T


@dataclass
class _RunState[T, R]:
    completed: list[TaskResult[R]]
    failures: list[TaskFailure]
    interrupted: list[TaskInterruption]
    futures: dict[Future[R], _InFlight[T]]
    next_index: int = 0
    drain_started_at: float | None = None
    drain_timed_out: bool = False


@dataclass(frozen=True)
class _RunPlan[T, R]:
    items: Sequence[T]
    worker: Callable[[T], R]
    task_name: Callable[[T], str]


@dataclass(frozen=True)
class BoundedWorkerRunner:
    """Run work with bounded threads and deterministic stop handling."""

    settings: ConcurrencySettings
    check_stop: Callable[[], None] = _no_stop_check
    event_sink: EventSink | None = None
    clock: Callable[[], float] = time.monotonic
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
        plan = _RunPlan(items, worker, task_name)
        state: _RunState[T, R] = _RunState([], [], [], {})
        executor = ThreadPoolExecutor(max_workers=self.settings.max_workers)
        wait_for_workers = True

        try:
            self._fill_workers(executor, state, plan)

            while state.futures:
                self._record_external_stop(state, plan)
                if self._drain_expired(state):
                    self._interrupt_remaining(state)
                    wait_for_workers = False
                    break

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
            executor.shutdown(wait=wait_for_workers, cancel_futures=True)

        return _finish_result(state)

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
            and state.next_index < len(plan.items)
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
                    state.next_index,
                    _pending_task_name(plan.items, state.next_index, plan.task_name),
                    error,
                ),
            )
            return

        item = plan.items[state.next_index]
        name = plan.task_name(item)
        state.futures[executor.submit(plan.worker, item)] = _InFlight(
            state.next_index,
            name,
            item,
        )
        self._emit("submitted", state.next_index, name)
        state.next_index += 1

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
                    state.next_index,
                    _pending_task_name(plan.items, state.next_index, plan.task_name),
                    error,
                ),
            )

    def _collect_result[T, R](
        self,
        future: Future[R],
        task: _InFlight[T],
        state: _RunState[T, R],
    ) -> None:
        try:
            result = future.result()
        except Exception as error:  # noqa: BLE001  # pylint: disable=broad-exception-caught
            self._record_failure(state, TaskFailure(task.index, task.name, error))
        else:
            state.completed.append(TaskResult(task.index, task.name, result))
            self._emit("completed", task.index, task.name)

    def _record_failure[T, R](
        self,
        state: _RunState[T, R],
        failure: TaskFailure,
    ) -> None:
        state.failures.append(failure)
        if state.drain_started_at is None:
            state.drain_started_at = self.clock()
        self._emit(
            "stop-requested" if isinstance(failure.error, GracefulStop) else "failed",
            failure.index,
            failure.name,
            str(failure.error),
        )

    def _drain_expired[T, R](self, state: _RunState[T, R]) -> bool:
        if not state.futures or state.drain_started_at is None:
            return False
        return (
            self.clock() - state.drain_started_at
        ) >= self.settings.stop_grace_seconds

    def _wait_timeout[T, R](self, state: _RunState[T, R]) -> float:
        if state.drain_started_at is None:
            return self.poll_interval
        remaining = self.settings.stop_grace_seconds - (
            self.clock() - state.drain_started_at
        )
        return max(0.0, min(self.poll_interval, remaining))

    def _interrupt_remaining[T, R](self, state: _RunState[T, R]) -> None:
        state.drain_timed_out = True
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
            else:
                reason = "drain-timeout"
                event_kind = "drain-timeout"
            state.interrupted.append(TaskInterruption(task.index, task.name, reason))
            self._emit(event_kind, task.index, task.name)
            state.futures.pop(future)

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


def _finish_result[T, R](state: _RunState[T, R]) -> BoundedRunResult[R]:
    state.completed.sort(key=lambda result: result.index)
    state.failures.sort(key=lambda failure: failure.index)
    state.interrupted.sort(key=lambda interruption: interruption.index)
    failures = tuple(state.failures)
    return BoundedRunResult(
        completed=tuple(state.completed),
        failure=failures[0] if failures else None,
        failures=failures,
        interrupted=tuple(state.interrupted),
        drain_timed_out=state.drain_timed_out,
    )


def _pending_task_name[T](
    items: Sequence[T],
    index: int,
    task_name: Callable[[T], str],
) -> str:
    if index >= len(items):
        return ""
    return task_name(items[index])


def _default_max_workers() -> int:
    cpu_count = os.cpu_count() or 1
    return max(1, cpu_count * 2)


def _env_positive_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None or not value.isdecimal():
        return default
    parsed = int(value)
    return parsed if parsed > 0 else default


def _env_positive_float(name: str, default: float) -> float:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        parsed = float(value)
    except ValueError:
        return default
    return parsed if parsed > 0 else default
