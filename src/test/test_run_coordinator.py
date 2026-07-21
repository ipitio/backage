"""Tests for top-level Python run coordination."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from bkg_py.orchestration import BatchRuntimeService
from bkg_py.owners.batch import OwnerBatchRequest
from bkg_py.result import ExitStatus
from bkg_py.run import (
    OwnerQueuePhaseRequest,
    RunCoordinator,
    RunCoordinatorExecution,
    RunCoordinatorRequest,
    RunMode,
)
from bkg_py.run_planning import PackageWorkPlanSummary
from bkg_py.run_startup import RunStartupResult
from bkg_py.runtime import GracefulStop
from bkg_py.state import StateStore


@dataclass(frozen=True)
class FakeRunOptions:
    """Configurable outcomes for fake coordinator phases."""

    fast_out: bool = False
    package_plan: PackageWorkPlanSummary | None = None
    stop_at: str | None = None
    owner_status: ExitStatus = ExitStatus.SUCCESS


class FakeRunPhases:  # pylint: disable=too-many-instance-attributes
    """Record coordinator calls while providing durable phase side effects."""

    def __init__(
        self,
        state: StateStore,
        options: FakeRunOptions | None = None,
    ) -> None:
        selected = options or FakeRunOptions()
        self.state = state
        self.fast_out = selected.fast_out
        self.package_plan = selected.package_plan or PackageWorkPlanSummary(10, 2, 8)
        self.stop_at = selected.stop_at
        self.owner_status = selected.owner_status
        self.events: list[str] = []
        self.owner_queue_request: OwnerQueuePhaseRequest | None = None
        self.materialized: tuple[str, ...] = ()
        self.owner_request: OwnerBatchRequest | None = None

    def prepare_run(self, request: RunCoordinatorRequest) -> RunStartupResult:
        """Return configured startup values and initialize batch state."""

        del request
        self._record("prepare")
        self.state.set_many(
            {
                "BKG_BATCH_FIRST_STARTED": "2026-07-12",
                "BKG_BATCH_MARKER": "batch-1",
                "BKG_TIMEOUT": 0,
            }
        )
        return RunStartupResult(
            "2026-07-12",
            self.package_plan,
            1_234,
            0,
            self.fast_out,
        )

    def discover_owners(
        self,
        today: str,
        skip_explore: bool,
        connections_file: Path,
        packages_all_file: Path,
    ) -> None:
        """Record a discovery phase."""

        del today, connections_file, packages_all_file
        self._record(f"discover:{str(skip_explore).lower()}")

    def prepare_optout_owner_queue(self) -> None:
        """Record and populate the opt-out owner queue."""

        self._record("optout-queue")
        self._queue_owners()

    def prepare_package_plan(
        self,
        since: str,
        working_directory: Path,
        *,
        reset: bool = False,
    ) -> PackageWorkPlanSummary:
        """Record and return package-plan publication."""

        del since, working_directory, reset
        self._record("package-plan")
        return self.package_plan

    def prepare_owner_queue(self, request: OwnerQueuePhaseRequest) -> None:
        """Record and populate the global owner queue."""

        self._record(f"owner-queue:{str(request.include_manual).lower()}")
        self.owner_queue_request = request
        self._queue_owners()

    def prepare_targeted_owner_queue(self, connections_file: Path) -> None:
        """Record and populate the targeted owner queue."""

        del connections_file
        self._record("targeted-queue")
        self._queue_owners()

    def materialize_owner_trees(self, owners: tuple[str, ...]) -> None:
        """Capture owner paths requested for materialization."""

        self._record("materialize")
        self.materialized = owners

    def update_owners(self, request: OwnerBatchRequest) -> ExitStatus:
        """Return the configured owner-batch status."""

        self._record("update")
        self.owner_request = request
        return self.owner_status

    def finalize_run(
        self,
        today: str,
        prepare_snapshot: bool,
        working_directory: Path,
    ) -> None:
        """Record finalization and snapshot selection."""

        del today, working_directory
        self._record(f"finalize:{str(prepare_snapshot).lower()}")

    def _queue_owners(self) -> None:
        self.state.set("BKG_OWNERS_QUEUE", r"1/one\n2/two\n3/one")

    def _record(self, event: str) -> None:
        self.events.append(event)
        if self.stop_at == event.split(":", maxsplit=1)[0]:
            self.state.set("BKG_TIMEOUT", 1)
            raise GracefulStop(event)


def _run(
    tmp_path: Path,
    mode: RunMode,
    *,
    github_owner: str = "example",
    source_published_today: bool = False,
    phases: FakeRunPhases | None = None,
) -> tuple[int, StateStore, FakeRunPhases, list[str], list[str]]:
    state = phases.state if phases is not None else StateStore(tmp_path / "state.env")
    selected_phases = phases or FakeRunPhases(state)
    progress: list[str] = []
    diagnostics: list[str] = []
    coordinator = RunCoordinator(
        state,
        selected_phases,
        RunCoordinatorExecution(progress.append, diagnostics.append, now=lambda: 2_000),
    )
    status = coordinator.run(
        RunCoordinatorRequest(
            today="2026-07-13",
            started_at=1_000,
            mode=mode,
            github_owner=github_owner,
            source_published_today=source_published_today,
            working_directory=tmp_path,
        )
    )
    return status, state, selected_phases, progress, diagnostics


@pytest.mark.parametrize(
    "mode",
    [
        RunMode.ALL_PUBLIC,
        RunMode.ALL_PUBLIC_AND_OWN_PRIVATE,
    ],
)
def test_global_modes_run_global_queue_owner_work_and_snapshot(
    tmp_path: Path,
    mode: RunMode,
) -> None:
    """Global modes share the complete discovery and publication sequence."""

    status, state, phases, progress, diagnostics = _run(tmp_path, mode)

    assert status == ExitStatus.SUCCESS
    assert phases.events == [
        "prepare",
        "discover:false",
        "owner-queue:true",
        "update",
        "finalize:true",
    ]
    assert phases.owner_request == OwnerBatchRequest(
        "2026-07-12",
        "batch-1",
        False,
    )
    assert state.get_int("BKG_DIFF") == 1_234
    assert state.get_int("BKG_REST_TO_TOP") == 1
    assert state.get("BKG_TIMEOUT") is None
    assert progress[:3] == ["all: 10", "done: 2", "left: 8"]
    assert not diagnostics


@pytest.mark.parametrize(
    "mode",
    [
        RunMode.OWN_PUBLIC,
        RunMode.OWN_PUBLIC_AND_PRIVATE,
        RunMode.OWN_PRIVATE,
    ],
)
def test_targeted_modes_run_membership_queue_owner_work_and_snapshot(
    tmp_path: Path,
    mode: RunMode,
) -> None:
    """Targeted modes share membership discovery and owner processing."""

    status, _, phases, _, _ = _run(tmp_path, mode)

    assert status == ExitStatus.SUCCESS
    assert phases.events == [
        "prepare",
        "discover:false",
        "targeted-queue",
        "update",
        "finalize:true",
    ]


def test_clean_mode_only_prepares_and_publishes_without_snapshot(
    tmp_path: Path,
) -> None:
    """Clean mode bypasses discovery and owner work while retaining summaries."""

    status, _, phases, _, _ = _run(tmp_path, RunMode.CLEAN)

    assert status == ExitStatus.SUCCESS
    assert phases.events == ["prepare", "finalize:false"]


def test_global_daily_gates_skip_completed_main_repo_work(tmp_path: Path) -> None:
    """Completed daily gates skip exploration and the manual owners file."""

    state = StateStore(tmp_path / "state.env")
    state.set_many({"BKG_BATCH_MARKER": "batch-1", "BKG_REST_TO_TOP": 0})
    runtime = BatchRuntimeService(state)
    runtime.complete_daily_gate("BKG_LAST_EXPLORE_DATE", "2026-07-13")
    runtime.complete_daily_gate("BKG_LAST_OWNERS_QUEUE_DATE", "2026-07-13")
    phases = FakeRunPhases(state)

    status, _, phases, progress, _ = _run(
        tmp_path,
        RunMode.ALL_PUBLIC,
        github_owner="ipitio",
        source_published_today=True,
        phases=phases,
    )

    assert status == ExitStatus.SUCCESS
    assert "discover:true" in phases.events
    assert "owner-queue:false" in phases.events
    assert "Skipping owners.txt queue; already ran today" in progress


def test_completed_batch_republishes_package_plan(tmp_path: Path) -> None:
    """A completed package target starts and publishes the next batch."""

    state = StateStore(tmp_path / "state.env")
    phases = FakeRunPhases(
        state,
        FakeRunOptions(package_plan=PackageWorkPlanSummary(2, 2, 0)),
    )

    status, state, phases, _, _ = _run(
        tmp_path,
        RunMode.ALL_PUBLIC,
        phases=phases,
    )

    assert status == ExitStatus.SUCCESS
    assert "package-plan" in phases.events
    assert state.get("BKG_BATCH_FIRST_STARTED") == "2026-07-13"
    assert state.get("BKG_BATCH_MARKER") != "batch-1"


def test_fast_optout_run_preserves_nonfatal_status_through_publication(
    tmp_path: Path,
) -> None:
    """Fast opt-out work publishes its changes and retains status one."""

    state = StateStore(tmp_path / "state.env")
    phases = FakeRunPhases(state, FakeRunOptions(fast_out=True))

    status, _, phases, _, _ = _run(
        tmp_path,
        RunMode.ALL_PUBLIC,
        phases=phases,
    )

    assert status == ExitStatus.NON_FATAL
    assert phases.events == [
        "prepare",
        "optout-queue",
        "update",
        "finalize:true",
    ]
    assert phases.owner_request is not None
    assert phases.owner_request.fast_out


@pytest.mark.parametrize("stop_at", ["discover", "owner-queue", "update"])
def test_graceful_phase_stop_still_finalizes_resumable_state(
    tmp_path: Path,
    stop_at: str,
) -> None:
    """Graceful stops skip later work but retain final snapshot publication."""

    state = StateStore(tmp_path / "state.env")
    phases = FakeRunPhases(state, FakeRunOptions(stop_at=stop_at))

    status, state, phases, _, diagnostics = _run(
        tmp_path,
        RunMode.ALL_PUBLIC,
        phases=phases,
    )

    assert status == ExitStatus.GRACEFUL_STOP
    assert phases.events[-1] == "finalize:true"
    assert state.get("BKG_TIMEOUT") is None
    assert any("Graceful stop requested" in message for message in diagnostics)


def test_owner_failure_aborts_before_finalization(tmp_path: Path) -> None:
    """Unexpected owner failures cannot publish a replacement snapshot."""

    state = StateStore(tmp_path / "state.env")
    phases = FakeRunPhases(
        state,
        FakeRunOptions(owner_status=ExitStatus.FAILURE),
    )

    status, _, phases, _, diagnostics = _run(
        tmp_path,
        RunMode.ALL_PUBLIC,
        phases=phases,
    )

    assert status == ExitStatus.FAILURE
    assert phases.events[-1] == "update"
    assert "finalize:true" not in phases.events
    assert diagnostics == [
        "Owner updates failed with status 2; stopping before snapshot publication."
    ]
