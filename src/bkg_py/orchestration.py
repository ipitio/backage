"""Typed run and batch decisions for the migrating application orchestrator."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Literal

from .result import ExitStatus
from .state import StateStore

MarkerFactory = Callable[[], str]
OwnerPhaseAction = Literal["publish", "abort"]
_BATCH_COMPLETION_LIMIT = 10_000
_PRIMARY_RATE_WINDOW_SECONDS = 3600
_SECONDARY_RATE_WINDOW_SECONDS = 60
_MAX_PROCESS_STATUS = 255
_DAILY_GATE_KEYS = frozenset(
    {
        "BKG_LAST_EXPLORE_DATE",
        "BKG_LAST_OWNERS_QUEUE_DATE",
    }
)


@dataclass(frozen=True)
class RunInitialization:
    """Durable values selected while beginning one application run."""

    batch_first_started: str
    batch_marker: str


@dataclass(frozen=True)
class BatchTransition:
    """Durable batch state after applying current work progress."""

    reset: bool
    batch_first_started: str


@dataclass(frozen=True)
class OwnerPhaseDecision:
    """Top-level action after the concurrent owner-update phase."""

    action: OwnerPhaseAction
    run_status: int
    message: str = ""


class RunOutcomePolicy:  # pylint: disable=too-few-public-methods
    """Map completed phase statuses into top-level publication decisions."""

    @staticmethod
    def owner_updates(phase_status: int, run_status: int = 0) -> OwnerPhaseDecision:
        """Preserve graceful publication while aborting unexpected failures."""

        _validate_process_status(phase_status, "owner phase")
        _validate_process_status(run_status, "run")
        if phase_status == 0:
            return OwnerPhaseDecision("publish", run_status)
        if phase_status == ExitStatus.GRACEFUL_STOP:
            return OwnerPhaseDecision(
                "publish",
                int(ExitStatus.GRACEFUL_STOP),
                "Reached BKG_MAX_LEN, stopping after persisting state...",
            )
        return OwnerPhaseDecision(
            "abort",
            phase_status,
            f"Owner updates failed with status {phase_status}; "
            "stopping before snapshot publication.",
        )


class BatchRuntimeService:
    """Own durable run, batch, and daily-gate decisions."""

    def __init__(self, state: StateStore) -> None:
        self.state = state

    def begin_run(
        self,
        today: str,
        started_at: int,
        *,
        marker_factory: MarkerFactory | None = None,
    ) -> RunInitialization:
        """Reset run-scoped state while preserving the active batch and rate windows."""

        _validate_run_start(today, started_at)
        snapshot = self.state.snapshot()
        batch_first_started = snapshot.get("BKG_BATCH_FIRST_STARTED") or today
        batch_marker = (
            snapshot.get("BKG_BATCH_MARKER")
            or (marker_factory or (lambda: _batch_marker(started_at)))()
        )
        rate_started_at, calls = _rate_window(
            snapshot,
            "BKG_RATE_LIMIT_START",
            "BKG_CALLS_TO_API",
            started_at,
            _PRIMARY_RATE_WINDOW_SECONDS,
        )
        minute_started_at, minute_calls = _rate_window(
            snapshot,
            "BKG_MIN_RATE_LIMIT_START",
            "BKG_MIN_CALLS_TO_API",
            started_at,
            _SECONDARY_RATE_WINDOW_SECONDS,
        )
        self.state.set_many(
            {
                "BKG_BATCH_FIRST_STARTED": batch_first_started,
                "BKG_BATCH_MARKER": batch_marker,
                "BKG_RATE_LIMIT_START": rate_started_at,
                "BKG_CALLS_TO_API": calls,
                "BKG_MIN_RATE_LIMIT_START": minute_started_at,
                "BKG_MIN_CALLS_TO_API": minute_calls,
                "BKG_LAST_SCANNED_ID": _state_int(
                    snapshot,
                    "BKG_LAST_SCANNED_ID",
                ),
                "BKG_DIFF": _state_int(snapshot, "BKG_DIFF"),
                "BKG_REST_TO_TOP": _state_int(snapshot, "BKG_REST_TO_TOP"),
                "BKG_DISCOVERED_CONNECTION_OWNERS": "",
                "BKG_OWNERS_QUEUE": "",
                "BKG_TIMEOUT": 0,
                "BKG_SCRIPT_START": started_at,
            }
        )
        return RunInitialization(batch_first_started, batch_marker)

    def complete_batch_if_exhausted(
        self,
        today: str,
        total: int,
        completed: int,
        *,
        marker_factory: MarkerFactory | None = None,
    ) -> BatchTransition:
        """Start a new batch once the active batch reaches its completion target."""

        _validate_date(today)
        _validate_package_counts(total, completed)
        if completed < _batch_completion_target(total):
            return BatchTransition(
                reset=False,
                batch_first_started=(
                    self.state.get("BKG_BATCH_FIRST_STARTED") or today
                ),
            )

        batch_marker = (marker_factory or _new_batch_marker)()
        self.state.set_many(
            {
                "BKG_BATCH_FIRST_STARTED": today,
                "BKG_BATCH_MARKER": batch_marker,
                "BKG_PACKAGE_PROGRESS_MARKER": batch_marker,
            }
        )
        return BatchTransition(reset=True, batch_first_started=today)

    def should_skip_daily_gate(
        self,
        key: str,
        today: str,
        *,
        source_published_today: bool,
    ) -> bool:
        """Return whether this batch context already completed a daily phase."""

        _validate_date(today)
        _validate_daily_gate_key(key)
        if not source_published_today:
            return False
        snapshot = self.state.snapshot()
        context, direction = _daily_gate_context(snapshot, today)
        completed = _completed_daily_gate_directions(snapshot.get(key), context)
        return direction in completed

    def complete_daily_gate(self, key: str, today: str) -> None:
        """Persist completion for the current date, batch, and queue direction."""

        _validate_date(today)
        _validate_daily_gate_key(key)
        snapshot = self.state.snapshot()
        context, direction = _daily_gate_context(snapshot, today)
        completed = _completed_daily_gate_directions(snapshot.get(key), context)
        completed.add(direction)
        value = f"{context}|{','.join(sorted(completed))}"
        if snapshot.get(key) != value:
            self.state.set(key, value)


def _validate_run_start(today: str, started_at: int) -> None:
    _validate_date(today)
    if started_at <= 0:
        raise ValueError("run start time must be positive")


def _validate_date(today: str) -> None:
    try:
        parsed = date.fromisoformat(today)
    except ValueError as error:
        raise ValueError(f"invalid UTC run date: {today}") from error
    if parsed.isoformat() != today:
        raise ValueError(f"invalid UTC run date: {today}")


def _validate_daily_gate_key(key: str) -> None:
    if key not in _DAILY_GATE_KEYS:
        raise ValueError(f"unsupported daily gate: {key}")


def _validate_process_status(status: int, label: str) -> None:
    if not 0 <= status <= _MAX_PROCESS_STATUS:
        raise ValueError(f"invalid {label} status: {status}")


def _validate_package_counts(total: int, completed: int) -> None:
    if total < 0:
        raise ValueError("total package count cannot be negative")
    if completed < 0:
        raise ValueError("completed package count cannot be negative")
    if completed > total:
        raise ValueError("completed package count cannot exceed total package count")


def _daily_gate_context(snapshot: Mapping[str, str], today: str) -> tuple[str, str]:
    batch_marker = (
        snapshot.get("BKG_BATCH_MARKER")
        or snapshot.get("BKG_BATCH_FIRST_STARTED")
        or "default"
    )
    rest_to_top = snapshot.get("BKG_REST_TO_TOP") or "0"
    return f"{today}|{batch_marker}", rest_to_top


def _completed_daily_gate_directions(value: str | None, context: str) -> set[str]:
    if not value:
        return set()
    stored_context, separator, directions = value.rpartition("|")
    if not separator or stored_context != context:
        return set()
    return {direction for direction in directions.split(",") if direction}


def _state_int(snapshot: Mapping[str, str], key: str, default: int = 0) -> int:
    try:
        return int(snapshot.get(key, str(default)))
    except ValueError:
        return default


def _rate_window(
    snapshot: Mapping[str, str],
    start_key: str,
    calls_key: str,
    started_at: int,
    duration: int,
) -> tuple[int, int]:
    window_started_at = _state_int(snapshot, start_key, started_at)
    calls = _state_int(snapshot, calls_key)
    if window_started_at <= 0 or window_started_at + duration <= started_at:
        return started_at, 0
    return window_started_at, max(0, calls)


def _batch_completion_target(total: int) -> int:
    return min(_BATCH_COMPLETION_LIMIT, total)


def _batch_marker(started_at: int) -> str:
    timestamp = datetime.fromtimestamp(started_at, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"{timestamp}-{os.getpid()}"


def _new_batch_marker() -> str:
    return _batch_marker(int(datetime.now(UTC).timestamp()))
