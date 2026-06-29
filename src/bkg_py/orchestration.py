"""Typed run and batch decisions for the migrating application orchestrator."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime

from .state import StateStore

MarkerFactory = Callable[[], str]
_PRIMARY_RATE_WINDOW_SECONDS = 3600
_SECONDARY_RATE_WINDOW_SECONDS = 60


@dataclass(frozen=True)
class RunInitialization:
    """Durable values selected while beginning one application run."""

    batch_first_started: str
    batch_marker: str


class BatchRuntimeService:  # pylint: disable=too-few-public-methods
    """Initialize one run through a single atomic persisted-state transition."""

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


def _validate_run_start(today: str, started_at: int) -> None:
    try:
        parsed = date.fromisoformat(today)
    except ValueError as error:
        raise ValueError(f"invalid UTC run date: {today}") from error
    if parsed.isoformat() != today:
        raise ValueError(f"invalid UTC run date: {today}")
    if started_at <= 0:
        raise ValueError("run start time must be positive")


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


def _batch_marker(started_at: int) -> str:
    timestamp = datetime.fromtimestamp(started_at, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"{timestamp}-{os.getpid()}"
