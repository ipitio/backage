"""Bounded worker execution for Python-owned bkg pipelines."""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass

from .runtime import GracefulStop

_DEFAULT_STOP_GRACE_SECONDS = 180.0


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
class BoundedRunResult[R]:
    """Completed work and the first task failure, if any."""

    completed: tuple[TaskResult[R], ...]
    failure: TaskFailure | None = None

    @property
    def stopped(self) -> bool:
        """Return whether the run ended because graceful stop was requested."""

        return isinstance(self.failure.error, GracefulStop) if self.failure else False

    @property
    def ok(self) -> bool:
        """Return whether every task completed successfully."""

        return self.failure is None


@dataclass(frozen=True)
class _InFlight[T]:
    index: int
    name: str
    item: T


@dataclass
class _RunState[T, R]:
    completed: list[TaskResult[R]]
    failures: list[TaskFailure]
    futures: dict[Future[R], _InFlight[T]]
    next_index: int = 0


def run_bounded[T, R](
    items: Sequence[T],
    worker: Callable[[T], R],
    *,
    max_workers: int,
    check_stop: Callable[[], None] = lambda: None,
    task_name: Callable[[T], str] = str,
) -> BoundedRunResult[R]:
    """Run items with bounded concurrency and deterministic completion records."""

    if max_workers <= 0:
        raise ValueError("max_workers must be greater than zero")

    state: _RunState[T, R] = _RunState([], [], {})

    def submit_next(executor: ThreadPoolExecutor) -> bool:
        if state.next_index >= len(items) or state.failures:
            return False
        try:
            check_stop()
        except GracefulStop as error:
            state.failures.append(TaskFailure(state.next_index, "", error))
            return False
        item = items[state.next_index]
        state.futures[executor.submit(worker, item)] = _InFlight(
            state.next_index,
            task_name(item),
            item,
        )
        state.next_index += 1
        return True

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        while len(state.futures) < max_workers and submit_next(executor):
            pass
        while state.futures:
            done, _pending = wait(state.futures, return_when=FIRST_COMPLETED)
            for future in done:
                task = state.futures.pop(future)
                _collect_result(future, task, state.completed, state.failures)
            while len(state.futures) < max_workers and submit_next(executor):
                pass

    state.completed.sort(key=lambda result: result.index)
    state.failures.sort(key=lambda failure: failure.index)
    return BoundedRunResult(
        completed=tuple(state.completed),
        failure=state.failures[0] if state.failures else None,
    )


def _collect_result[T, R](
    future: Future[R],
    task: _InFlight[T],
    completed: list[TaskResult[R]],
    failures: list[TaskFailure],
) -> None:
    try:
        completed.append(TaskResult(task.index, task.name, future.result()))
    except Exception as error:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        failures.append(TaskFailure(task.index, task.name, error))


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
