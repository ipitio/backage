"""Tests for Python-owned run and batch orchestration decisions."""

from __future__ import annotations

from pathlib import Path

import pytest

from bkg_py.orchestration import BatchRuntimeService, RunOutcomePolicy
from bkg_py.state import StateStore


def test_begin_run_initializes_state_atomically(tmp_path: Path) -> None:
    """A fresh run receives defaults and a durable batch identity."""

    state = StateStore(tmp_path / "state.env")

    result = BatchRuntimeService(state).begin_run(
        "2026-06-29",
        1_000,
        marker_factory=lambda: "batch-1",
    )

    assert result.batch_first_started == "2026-06-29"
    assert result.batch_marker == "batch-1"
    assert state.snapshot() == {
        "BKG_BATCH_FIRST_STARTED": "2026-06-29",
        "BKG_BATCH_MARKER": "batch-1",
        "BKG_RATE_LIMIT_START": "1000",
        "BKG_CALLS_TO_API": "0",
        "BKG_MIN_RATE_LIMIT_START": "1000",
        "BKG_MIN_CALLS_TO_API": "0",
        "BKG_LAST_SCANNED_ID": "0",
        "BKG_DIFF": "0",
        "BKG_REST_TO_TOP": "0",
        "BKG_DISCOVERED_CONNECTION_OWNERS": "",
        "BKG_TIMEOUT": "0",
        "BKG_SCRIPT_START": "1000",
    }


def test_begin_run_preserves_batch_and_active_rate_windows(tmp_path: Path) -> None:
    """Restarting does not discard the batch or unexpired request accounting."""

    state = StateStore(tmp_path / "state.env")
    state.set_many(
        {
            "BKG_BATCH_FIRST_STARTED": "2026-06-28",
            "BKG_BATCH_MARKER": "batch-existing",
            "BKG_RATE_LIMIT_START": 900,
            "BKG_CALLS_TO_API": 17,
            "BKG_MIN_RATE_LIMIT_START": 950,
            "BKG_MIN_CALLS_TO_API": 7,
            "BKG_OWNERS_QUEUE": r"1/one\n2/two",
            "BKG_TIMEOUT": 1,
        }
    )

    result = BatchRuntimeService(state).begin_run("2026-06-29", 1_000)

    assert result.batch_first_started == "2026-06-28"
    assert result.batch_marker == "batch-existing"
    assert state.get_int("BKG_RATE_LIMIT_START") == 900
    assert state.get_int("BKG_CALLS_TO_API") == 17
    assert state.get_int("BKG_MIN_RATE_LIMIT_START") == 950
    assert state.get_int("BKG_MIN_CALLS_TO_API") == 7
    assert state.get("BKG_OWNERS_QUEUE") == r"1/one\n2/two"
    assert state.get("BKG_TIMEOUT") == "0"


def test_begin_run_resets_expired_or_invalid_rate_windows(tmp_path: Path) -> None:
    """Expired and malformed request counters cannot poison a later run."""

    state = StateStore(tmp_path / "state.env")
    state.set_many(
        {
            "BKG_RATE_LIMIT_START": 1_000,
            "BKG_CALLS_TO_API": 900,
            "BKG_MIN_RATE_LIMIT_START": "invalid",
            "BKG_MIN_CALLS_TO_API": -4,
        }
    )

    BatchRuntimeService(state).begin_run(
        "2026-06-29",
        5_000,
        marker_factory=lambda: "batch-1",
    )

    assert state.get_int("BKG_RATE_LIMIT_START") == 5_000
    assert state.get_int("BKG_CALLS_TO_API") == 0
    assert state.get_int("BKG_MIN_RATE_LIMIT_START") == 5_000
    assert state.get_int("BKG_MIN_CALLS_TO_API") == 0


def test_complete_batch_if_exhausted_resets_only_at_completion_target(
    tmp_path: Path,
) -> None:
    """An active batch stays put until completed work reaches its capped target."""

    state = StateStore(tmp_path / "state.env")
    state.set_many(
        {
            "BKG_BATCH_FIRST_STARTED": "2026-06-28",
            "BKG_BATCH_MARKER": "batch-existing",
            "BKG_UNKNOWN": "preserved",
        }
    )
    service = BatchRuntimeService(state)

    active = service.complete_batch_if_exhausted("2026-06-29", 10_001, 9_999)

    assert not active.reset
    assert active.batch_first_started == "2026-06-28"
    assert state.get("BKG_BATCH_MARKER") == "batch-existing"

    completed = service.complete_batch_if_exhausted(
        "2026-06-29",
        10_001,
        10_000,
        marker_factory=lambda: "batch-next",
    )

    assert completed.reset
    assert completed.batch_first_started == "2026-06-29"
    assert state.get("BKG_BATCH_FIRST_STARTED") == "2026-06-29"
    assert state.get("BKG_BATCH_MARKER") == "batch-next"
    assert state.get("BKG_PACKAGE_PROGRESS_MARKER") == "batch-next"
    assert state.get("BKG_UNKNOWN") == "preserved"


def test_complete_batch_if_exhausted_uses_total_when_below_cap(
    tmp_path: Path,
) -> None:
    """Small batches roll only after every package in that batch completes."""

    state = StateStore(tmp_path / "state.env")
    state.set_many(
        {
            "BKG_BATCH_FIRST_STARTED": "2026-06-28",
            "BKG_BATCH_MARKER": "batch-existing",
        }
    )
    service = BatchRuntimeService(state)

    active = service.complete_batch_if_exhausted("2026-06-29", 3, 2)

    assert not active.reset
    assert state.get("BKG_BATCH_MARKER") == "batch-existing"

    completed = service.complete_batch_if_exhausted(
        "2026-06-29",
        3,
        3,
        marker_factory=lambda: "batch-next",
    )

    assert completed.reset
    assert state.get("BKG_BATCH_MARKER") == "batch-next"


def test_daily_gate_tracks_date_batch_directions_and_source_publish(
    tmp_path: Path,
) -> None:
    """Daily phases complete once per queue direction in each batch context."""

    state = StateStore(tmp_path / "state.env")
    state.set_many(
        {
            "BKG_BATCH_MARKER": "batch-1",
            "BKG_REST_TO_TOP": 0,
        }
    )
    service = BatchRuntimeService(state)
    key = "BKG_LAST_EXPLORE_DATE"
    service.complete_daily_gate(key, "2026-06-29")

    assert not service.should_skip_daily_gate(
        key,
        "2026-06-29",
        source_published_today=False,
    )
    assert service.should_skip_daily_gate(
        key,
        "2026-06-29",
        source_published_today=True,
    )

    state.set("BKG_BATCH_MARKER", "batch-2")
    assert not service.should_skip_daily_gate(
        key,
        "2026-06-29",
        source_published_today=True,
    )
    service.complete_daily_gate(key, "2026-06-29")
    state.set("BKG_REST_TO_TOP", 1)
    assert not service.should_skip_daily_gate(
        key,
        "2026-06-29",
        source_published_today=True,
    )
    service.complete_daily_gate(key, "2026-06-29")
    state.set("BKG_REST_TO_TOP", 0)
    assert service.should_skip_daily_gate(
        key,
        "2026-06-29",
        source_published_today=True,
    )
    state.set("BKG_REST_TO_TOP", 1)
    assert service.should_skip_daily_gate(
        key,
        "2026-06-29",
        source_published_today=True,
    )
    assert state.get(key) == "2026-06-29|batch-2|0,1"
    assert not service.should_skip_daily_gate(
        key,
        "2026-06-30",
        source_published_today=True,
    )

    with pytest.raises(ValueError, match="unsupported daily gate"):
        service.complete_daily_gate("BKG_UNRELATED", "2026-06-29")


@pytest.mark.parametrize(
    ("phase_status", "run_status", "action", "decided_status", "message"),
    [
        (0, 0, "publish", 0, ""),
        (0, 3, "publish", 3, ""),
        (3, 0, "publish", 3, "Graceful stop requested"),
        (1, 0, "abort", 1, "stopping before snapshot publication"),
        (2, 0, "abort", 2, "stopping before snapshot publication"),
    ],
)
def test_owner_phase_policy_controls_snapshot_publication(
    phase_status: int,
    run_status: int,
    action: str,
    decided_status: int,
    message: str,
) -> None:
    """Only graceful stops remain publishable after a nonzero owner phase."""

    decision = RunOutcomePolicy.owner_updates(phase_status, run_status)

    assert decision.action == action
    assert decision.run_status == decided_status
    assert message in decision.message
