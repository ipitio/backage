"""Tests for Python-owned run and batch orchestration decisions."""

from __future__ import annotations

from pathlib import Path

import pytest

from bkg_py import ExitStatus
from bkg_py.cli import main
from bkg_py.database import DatabaseRepository, DatabaseSettings
from bkg_py.database_models import PackageRecord, PackageRef
from bkg_py.orchestration import BatchRuntimeService, RunOutcomePolicy
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


def test_complete_batch_if_exhausted_resets_only_finished_work(tmp_path: Path) -> None:
    """An unfinished tail preserves the batch; exhaustion replaces it atomically."""

    state = StateStore(tmp_path / "state.env")
    state.set_many(
        {
            "BKG_BATCH_FIRST_STARTED": "2026-06-28",
            "BKG_BATCH_MARKER": "batch-existing",
            "BKG_UNKNOWN": "preserved",
        }
    )
    service = BatchRuntimeService(state)

    active = service.complete_batch_if_exhausted("2026-06-29", 1)

    assert not active.reset
    assert active.batch_first_started == "2026-06-28"
    assert state.get("BKG_BATCH_MARKER") == "batch-existing"

    completed = service.complete_batch_if_exhausted(
        "2026-06-29",
        0,
        marker_factory=lambda: "batch-next",
    )

    assert completed.reset
    assert completed.batch_first_started == "2026-06-29"
    assert state.get("BKG_BATCH_FIRST_STARTED") == "2026-06-29"
    assert state.get("BKG_BATCH_MARKER") == "batch-next"
    assert state.get("BKG_UNKNOWN") == "preserved"


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
        (3, 0, "publish", 3, "Reached BKG_MAX_LEN"),
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

    status = main(
        [
            "orchestration",
            "complete-batch-if-exhausted",
            "2026-06-30",
            "0",
        ]
    )
    assert status == ExitStatus.SUCCESS
    assert capsys.readouterr().out == "true\t2026-06-30\n"

    status = main(["orchestration", "owner-phase-decision", "3", "0"])
    assert status == ExitStatus.SUCCESS
    assert capsys.readouterr().out == (
        "publish\t3\tReached BKG_MAX_LEN, stopping after persisting state...\n"
    )

    status = main(
        [
            "orchestration",
            "daily-gate-should-skip",
            "BKG_LAST_EXPLORE_DATE",
            "2026-06-30",
            "false",
        ]
    )
    assert status == ExitStatus.NON_FATAL
    assert capsys.readouterr().out == ""

    status = main(
        [
            "orchestration",
            "complete-daily-gate",
            "BKG_LAST_EXPLORE_DATE",
            "2026-06-30",
        ]
    )
    assert status == ExitStatus.SUCCESS
    assert capsys.readouterr().out == ""

    status = main(
        [
            "orchestration",
            "daily-gate-should-skip",
            "BKG_LAST_EXPLORE_DATE",
            "2026-06-30",
            "true",
        ]
    )
    assert status == ExitStatus.SUCCESS


def test_prepare_package_plan_cli_uses_the_configured_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The launcher receives counts while compatibility files are published."""

    database_path = tmp_path / "index.db"
    repository = DatabaseRepository(DatabaseSettings(database_path))
    package = PackageRef(
        "1",
        "users",
        "container",
        "Alpha",
        "repo",
        "package",
    )
    repository.write_package(PackageRecord(package, 1, 1, 1, 1, 1, "2026-06-29"))
    monkeypatch.setenv("BKG_ROOT", str(tmp_path))
    monkeypatch.setenv("BKG_ENV", str(tmp_path / "state.env"))
    monkeypatch.setenv("BKG_INDEX_DB", str(database_path))
    output = tmp_path / "plan"

    status = main(
        [
            "orchestration",
            "prepare-package-plan",
            "2026-06-29",
            str(output),
        ]
    )

    assert status == ExitStatus.SUCCESS
    assert capsys.readouterr().out == "1\t1\t0\n"
    assert (output / "packages_already_updated").is_file()
    assert (output / "packages_to_update").read_text(encoding="utf-8") == ""


def test_prepare_run_cli_prints_validated_startup_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The launcher receives one compact summary after startup preparation."""

    database_path = tmp_path / "index.db"
    repository = DatabaseRepository(DatabaseSettings(database_path))
    package = PackageRef(
        "1",
        "users",
        "container",
        "Alpha",
        "repo",
        "package",
    )
    repository.write_package(PackageRecord(package, 1, 1, 1, 1, 1, "2026-06-28"))
    optouts = tmp_path / "optout.txt"
    optouts.write_text("Owner\n", encoding="utf-8")
    state_path = tmp_path / "state.env"
    StateStore(state_path).set("BKG_OUT", 0)
    monkeypatch.setenv("BKG_ROOT", str(tmp_path))
    monkeypatch.setenv("BKG_ENV", str(state_path))
    monkeypatch.setenv("BKG_INDEX_DB", str(database_path))
    monkeypatch.setenv("BKG_OPTOUT", str(optouts))
    monkeypatch.setenv("BKG_OWNER_ID_CACHE", str(tmp_path / "owner-cache.txt"))

    status = main(
        [
            "orchestration",
            "prepare-run",
            "2026-06-29",
            "1000",
            str(tmp_path / "plan"),
        ]
    )

    captured = capsys.readouterr()
    values = captured.out.strip().split("\t")
    assert status == ExitStatus.SUCCESS
    assert values[:4] == ["2026-06-29", "1", "0", "1"]
    assert values[4].isdecimal()
    assert values[5:] == ["1", "true"]
    assert "prepare-package-state" in captured.err
