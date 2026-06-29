"""Tests for Python-owned run and batch orchestration decisions."""

from __future__ import annotations

from pathlib import Path

import pytest

from bkg_py import ExitStatus
from bkg_py.cli import main
from bkg_py.orchestration import BatchRuntimeService
from bkg_py.state import StateStore


def test_begin_run_initializes_state_atomically(tmp_path: Path) -> None:
    """A fresh run receives defaults, a batch identity, and empty run queues."""

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
        "BKG_OWNERS_QUEUE": "",
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
    assert state.get("BKG_OWNERS_QUEUE") == ""
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


def test_begin_run_cli_uses_the_configured_state_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The Bash-facing command prints the selected batch start date."""

    state_path = tmp_path / "state.env"
    monkeypatch.setenv("BKG_ROOT", str(tmp_path))
    monkeypatch.setenv("BKG_ENV", str(state_path))

    status = main(["orchestration", "begin-run", "2026-06-29", "1000"])

    assert status == ExitStatus.SUCCESS
    assert capsys.readouterr().out == "2026-06-29\n"
    assert StateStore(state_path).get("BKG_SCRIPT_START") == "1000"
