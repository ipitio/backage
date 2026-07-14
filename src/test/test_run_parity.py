"""Parity tests for the legacy Bash main and Python run coordinator."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from bkg_py.orchestration import BatchRuntimeService
from bkg_py.owner_batch import OwnerBatchRequest
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
class ParityScenario:
    """Outcomes supplied to both run implementations."""

    mode: RunMode
    fast_out: bool = False
    stop_at: str = "none"
    owner_status: ExitStatus = ExitStatus.SUCCESS
    batch_reset: bool = False
    daily_skip: bool = False


@dataclass(frozen=True)
class ParityResult:
    """Observable run behavior compared across implementations."""

    status: int
    events: tuple[str, ...]
    state: dict[str, str]


class ParityPhases:  # pylint: disable=too-many-instance-attributes
    """Provide deterministic phase behavior to the Python coordinator."""

    def __init__(self, state: StateStore, scenario: ParityScenario) -> None:
        self.state = state
        self.scenario = scenario
        self.events: list[str] = []
        self.package_plan = (
            PackageWorkPlanSummary(2, 2, 0)
            if scenario.batch_reset
            else PackageWorkPlanSummary(10, 2, 8)
        )

    def prepare_run(self, request: RunCoordinatorRequest) -> RunStartupResult:
        """Initialize the same batch values emitted by the shell fixture."""

        del request
        self._record("prepare")
        self.state.set_many(
            {
                "BKG_BATCH_FIRST_STARTED": "2026-07-12",
                "BKG_BATCH_MARKER": "batch-1",
                "BKG_REST_TO_TOP": 0,
                "BKG_TIMEOUT": 0,
            }
        )
        return RunStartupResult(
            "2026-07-12",
            self.package_plan,
            1_234,
            0,
            self.scenario.fast_out,
        )

    def discover_owners(
        self,
        today: str,
        skip_explore: bool,
        connections_file: Path,
        packages_all_file: Path,
    ) -> None:
        """Record global or targeted discovery."""

        del today, connections_file, packages_all_file
        self._record(f"discover:{str(skip_explore).lower()}", "discover")

    def prepare_optout_owner_queue(self) -> None:
        """Record and populate fast opt-out work."""

        self._record("optout-queue", "optout-queue")
        self._queue_owners()

    def prepare_package_plan(
        self,
        since: str,
        working_directory: Path,
        *,
        reset: bool = False,
    ) -> PackageWorkPlanSummary:
        """Record package-plan publication after rollover."""

        del since, working_directory, reset
        self._record("package-plan")
        return self.package_plan

    def prepare_owner_queue(self, request: OwnerQueuePhaseRequest) -> None:
        """Record and populate global owner work."""

        self._record(
            f"owner-queue:{str(request.include_manual).lower()}",
            "owner-queue",
        )
        self._queue_owners()

    def prepare_targeted_owner_queue(self, connections_file: Path) -> None:
        """Record and populate targeted owner work."""

        del connections_file
        self._record("targeted-queue", "targeted-queue")
        self._queue_owners()

    def materialize_owner_trees(self, owners: tuple[str, ...]) -> None:
        """Record sparse owner-tree materialization."""

        assert owners == ("one", "two")
        self._record("materialize")

    def update_owners(self, request: OwnerBatchRequest) -> ExitStatus:
        """Return the configured owner-batch status."""

        del request
        self._record("update", "update")
        return self.scenario.owner_status

    def finalize_run(
        self,
        today: str,
        prepare_snapshot: bool,
        working_directory: Path,
    ) -> None:
        """Record final summary and snapshot selection."""

        del today, working_directory
        self._record(f"finalize:{str(prepare_snapshot).lower()}")

    def _queue_owners(self) -> None:
        self.state.set("BKG_OWNERS_QUEUE", r"1/one\n2/two\n3/one")

    def _record(self, event: str, stop_name: str | None = None) -> None:
        self.events.append(event)
        if self.scenario.stop_at == stop_name:
            self.state.set("BKG_TIMEOUT", 1)
            raise GracefulStop(stop_name or event)


@pytest.mark.parametrize(
    "scenario",
    [
        *(ParityScenario(mode) for mode in RunMode),
        ParityScenario(RunMode.ALL_PUBLIC, fast_out=True),
        ParityScenario(RunMode.ALL_PUBLIC, batch_reset=True),
        ParityScenario(RunMode.ALL_PUBLIC, daily_skip=True),
        ParityScenario(RunMode.ALL_PUBLIC, stop_at="discover"),
        ParityScenario(RunMode.ALL_PUBLIC, stop_at="owner-queue"),
        ParityScenario(RunMode.ALL_PUBLIC, stop_at="update"),
        ParityScenario(RunMode.OWN_PUBLIC, stop_at="discover"),
        ParityScenario(RunMode.OWN_PUBLIC, stop_at="targeted-queue"),
        ParityScenario(
            RunMode.ALL_PUBLIC,
            owner_status=ExitStatus.FAILURE,
        ),
    ],
)
def test_python_coordinator_matches_legacy_main(
    tmp_path: Path,
    scenario: ParityScenario,
) -> None:
    """Both run engines preserve phase order, status, and durable scalar state."""

    assert _run_python(tmp_path / "python", scenario) == _run_bash(
        tmp_path / "bash",
        scenario,
    )


def _run_python(root: Path, scenario: ParityScenario) -> ParityResult:
    root.mkdir(parents=True)
    state = StateStore(root / "state.env")
    if scenario.daily_skip:
        state.set_many({"BKG_BATCH_MARKER": "batch-1", "BKG_REST_TO_TOP": 0})
        runtime = BatchRuntimeService(state)
        runtime.complete_daily_gate("BKG_LAST_EXPLORE_DATE", "2026-07-13")
        runtime.complete_daily_gate("BKG_LAST_OWNERS_QUEUE_DATE", "2026-07-13")
    phases = ParityPhases(state, scenario)
    coordinator = RunCoordinator(
        state,
        phases,
        RunCoordinatorExecution(lambda _message: None, lambda _message: None),
    )
    status = coordinator.run(
        RunCoordinatorRequest(
            today="2026-07-13",
            started_at=1_000,
            mode=scenario.mode,
            github_owner="ipitio" if scenario.daily_skip else "example",
            source_published_today=scenario.daily_skip,
            working_directory=root,
        )
    )
    return ParityResult(status, tuple(phases.events), _selected_state(state))


def _run_bash(root: Path, scenario: ParityScenario) -> ParityResult:
    root.mkdir(parents=True)
    repo = Path(__file__).resolve().parents[2]
    bash = shutil.which("bash")
    assert bash is not None
    result = subprocess.run(  # noqa: S603
        (
            bash,
            str(repo / "src/test/fixtures/run-parity.sh"),
            str(repo),
            str(root),
            str(int(scenario.mode)),
            str(scenario.fast_out).lower(),
            scenario.stop_at,
            str(int(scenario.owner_status)),
            str(scenario.batch_reset).lower(),
            str(scenario.daily_skip).lower(),
        ),
        check=True,
        capture_output=True,
        text=True,
    )
    status = 0
    events: list[str] = []
    state: dict[str, str] = {}
    for line in result.stdout.splitlines():
        kind, *values = line.split("\t")
        if kind == "status":
            status = int(values[0])
        elif kind == "event":
            events.append(values[0])
        elif kind == "state":
            state[values[0]] = values[1]
    return ParityResult(status, tuple(events), state)


def _selected_state(state: StateStore) -> dict[str, str]:
    snapshot = state.snapshot()
    return {
        key: snapshot.get(key, "<missing>")
        for key in ("BKG_DIFF", "BKG_REST_TO_TOP", "BKG_TIMEOUT")
    }
