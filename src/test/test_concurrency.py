"""Tests for bounded Python worker execution."""

from __future__ import annotations

import threading
import time

import pytest

from bkg_py.concurrency import (
    BoundedRunResult,
    BoundedWorkerRunner,
    ConcurrencySettings,
    WorkerEvent,
    run_bounded,
)
from bkg_py.config import RuntimeConfig
from bkg_py.runtime import GracefulStop


def test_concurrency_settings_default_to_cpu_scaled_workers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default worker count matches the shell async default shape."""

    monkeypatch.delenv("BKG_PARALLEL_ASYNC_MAX_JOBS", raising=False)
    monkeypatch.delenv("BKG_OWNER_UPDATE_STOP_GRACE", raising=False)
    monkeypatch.setattr("os.cpu_count", lambda: 4)

    settings = ConcurrencySettings.from_config(RuntimeConfig.from_env())

    assert settings.max_workers == 8
    assert settings.stop_grace_seconds == 180.0


def test_concurrency_settings_honor_explicit_env_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit tuning inputs are carried into Python worker settings."""

    monkeypatch.setenv("BKG_PARALLEL_ASYNC_MAX_JOBS", "9")
    monkeypatch.setenv("BKG_OWNER_UPDATE_STOP_GRACE", "12.5")
    monkeypatch.setattr("os.cpu_count", lambda: 4)

    settings = ConcurrencySettings.from_config(RuntimeConfig.from_env())

    assert settings.max_workers == 9
    assert settings.stop_grace_seconds == 12.5


def test_concurrency_settings_ignore_invalid_env_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invalid tuning values fall back to safe defaults."""

    monkeypatch.setenv("BKG_PARALLEL_ASYNC_MAX_JOBS", "0")
    monkeypatch.setenv("BKG_OWNER_UPDATE_STOP_GRACE", "nope")
    monkeypatch.setattr("os.cpu_count", lambda: None)

    settings = ConcurrencySettings.from_config(RuntimeConfig.from_env())

    assert settings.max_workers == 2
    assert settings.stop_grace_seconds == 180.0


def test_run_bounded_preserves_result_order() -> None:
    """Results are returned in input order even when completion order differs."""

    def worker(value: int) -> int:
        if value == 1:
            time.sleep(0.05)
        return value * 10

    result = run_bounded([1, 2, 3], worker, max_workers=3, task_name=str)

    assert result.ok
    assert [task.value for task in result.completed] == [10, 20, 30]
    assert [task.name for task in result.completed] == ["1", "2", "3"]


def test_runner_emits_structured_task_events() -> None:
    """Worker events carry stable task context for future Action diagnostics."""

    events: list[WorkerEvent] = []
    runner = BoundedWorkerRunner(
        ConcurrencySettings(max_workers=1),
        event_sink=events.append,
    )

    result = runner.run(["alpha"], str.upper, task_name=lambda value: f"owner:{value}")

    assert result.ok
    assert [(event.kind, event.index, event.name) for event in events] == [
        ("submitted", 0, "owner:alpha"),
        ("completed", 0, "owner:alpha"),
    ]


def test_run_bounded_limits_active_workers() -> None:
    """No more than the configured worker count runs at once."""

    active = 0
    max_active = 0
    lock = threading.Lock()

    def worker(value: int) -> int:
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.02)
        with lock:
            active -= 1
        return value

    result = run_bounded(list(range(8)), worker, max_workers=3)

    assert result.ok
    assert len(result.completed) == 8
    assert max_active <= 3


def test_runner_bounds_submitted_queue_growth() -> None:
    """The runner submits only enough work to fill the active worker window."""

    submitted: list[int] = []
    submitted_three = threading.Event()
    release = threading.Event()

    def record_event(event: WorkerEvent) -> None:
        if event.kind == "submitted" and event.index is not None:
            submitted.append(event.index)
            if len(submitted) == 3:
                submitted_three.set()

    def worker(value: int) -> int:
        release.wait(timeout=5)
        return value

    runner = BoundedWorkerRunner(
        ConcurrencySettings(max_workers=3),
        event_sink=record_event,
    )
    result_holder: list[BoundedRunResult[int]] = []

    def run_workers() -> None:
        result_holder.append(runner.run(list(range(8)), worker))

    thread = threading.Thread(target=run_workers)

    thread.start()
    try:
        assert submitted_three.wait(timeout=2)
        time.sleep(0.05)
        assert submitted == [0, 1, 2]
    finally:
        release.set()
        thread.join(timeout=5)

    assert not thread.is_alive()
    assert result_holder
    assert result_holder[0].ok


def test_run_bounded_stops_submitting_after_failure() -> None:
    """A task failure stops new submissions while retaining completed results."""

    started: list[int] = []

    def worker(value: int) -> int:
        started.append(value)
        if value == 2:
            raise ValueError("boom")
        return value

    result = run_bounded([1, 2, 3], worker, max_workers=1)

    assert not result.ok
    assert result.failure is not None
    assert result.failure.name == "2"
    assert isinstance(result.failure.error, ValueError)
    assert [task.value for task in result.completed] == [1]
    assert started == [1, 2]


def test_runner_drains_completed_work_after_graceful_stop() -> None:
    """Completed work remains available when later submission observes a stop."""

    checks = 0
    events: list[WorkerEvent] = []

    def check_stop() -> None:
        nonlocal checks
        checks += 1
        if checks > 1:
            raise GracefulStop("elapsed")

    runner = BoundedWorkerRunner(
        ConcurrencySettings(max_workers=1),
        check_stop=check_stop,
        event_sink=events.append,
    )

    result = runner.run([1, 2, 3], lambda value: value * 10, task_name=str)

    assert result.stopped
    assert result.failure is not None
    assert [task.value for task in result.completed] == [10]
    assert [event.kind for event in events].count("completed") == 1
    assert [event.kind for event in events].count("stop-requested") == 1


def test_runner_reports_one_stop_transition_for_concurrent_workers() -> None:
    """Concurrent stop failures enter one shared drain transition."""

    barrier = threading.Barrier(2)
    events: list[WorkerEvent] = []

    def worker(value: int) -> int:
        barrier.wait(timeout=2)
        raise GracefulStop(f"persisted by {value}")

    result = BoundedWorkerRunner(
        ConcurrencySettings(max_workers=2),
        event_sink=events.append,
    ).run([1, 2], worker, task_name=str)

    assert result.stopped
    assert len(result.failures) == 2
    assert [event.kind for event in events].count("stop-requested") == 1


def test_run_bounded_reports_graceful_stop_before_new_work() -> None:
    """A stop check prevents later tasks from starting."""

    checks = 0
    started: list[int] = []

    def check_stop() -> None:
        nonlocal checks
        checks += 1
        if checks > 1:
            raise GracefulStop("elapsed")

    def worker(value: int) -> int:
        started.append(value)
        return value

    result = run_bounded(
        [1, 2, 3],
        worker,
        max_workers=1,
        check_stop=check_stop,
    )

    assert result.stopped
    assert result.failure is not None
    assert isinstance(result.failure.error, GracefulStop)
    assert [task.value for task in result.completed] == [1]
    assert started == [1]


def test_runner_reports_drain_timeout_for_blocked_worker() -> None:
    """An overdue worker is reported but cannot outlive the runner."""

    stop_requested = threading.Event()
    worker_started = threading.Event()
    release_worker = threading.Event()
    worker_exited = threading.Event()
    runner_exited = threading.Event()
    events: list[WorkerEvent] = []
    results: list[BoundedRunResult[int]] = []

    def check_stop() -> None:
        if stop_requested.is_set():
            raise GracefulStop("elapsed")

    def worker(value: int) -> int:
        worker_started.set()
        stop_requested.set()
        try:
            release_worker.wait(timeout=5)
            return value
        finally:
            worker_exited.set()

    runner = BoundedWorkerRunner(
        ConcurrencySettings(max_workers=1, stop_grace_seconds=0.02),
        check_stop=check_stop,
        event_sink=events.append,
        poll_interval=0.005,
    )

    def run() -> None:
        results.append(runner.run([1, 2], worker, task_name=str))
        runner_exited.set()

    thread = threading.Thread(target=run)
    thread.start()
    assert worker_started.wait(timeout=2)
    assert not runner_exited.wait(timeout=0.1)
    release_worker.set()
    thread.join(timeout=2)

    assert runner_exited.is_set()
    assert worker_exited.is_set()
    result = results[0]
    assert result.stopped
    assert result.drain_timed_out
    assert result.interrupted
    assert result.interrupted[0].reason == "drain-timeout"
    assert [event.kind for event in events] == [
        "submitted",
        "stop-requested",
        "drain-timeout",
        "completed",
    ]


def test_run_bounded_rejects_empty_worker_limit() -> None:
    """Worker limits must be positive."""

    with pytest.raises(ValueError, match="max_workers"):
        run_bounded([1], lambda value: value, max_workers=0)
