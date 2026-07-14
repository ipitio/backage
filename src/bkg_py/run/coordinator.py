"""Top-level mode and phase ordering for one Bkg run."""

from __future__ import annotations

import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import IntEnum, unique
from pathlib import Path
from typing import Protocol

from ..orchestration import (
    BatchRuntimeService,
    OwnerPhaseDecision,
    RunOutcomePolicy,
)
from ..owner_batch import OwnerBatchRequest, parse_owner_queue
from ..result import ExitStatus
from ..run_planning import PackageWorkPlanSummary
from ..run_startup import RunStartupResult
from ..runtime import GracefulStop
from ..state import StateStore

MessageSink = Callable[[str], None]
Clock = Callable[[], int]
_PLANNING_FILES = (
    "all_owners_in_db",
    "all_owners_tu",
    "owners_updated",
    "owners_partially_updated",
    "owners_stale",
    "owners_scanned_without_packages",
)
_EXPLORE_GATE = "BKG_LAST_EXPLORE_DATE"
_OWNER_QUEUE_GATE = "BKG_LAST_OWNERS_QUEUE_DATE"


@unique
class RunMode(IntEnum):
    """Supported application modes."""

    ALL_PUBLIC = 0
    OWN_PUBLIC = 1
    CLEAN = 2
    ALL_PUBLIC_AND_OWN_PRIVATE = 3
    OWN_PUBLIC_AND_PRIVATE = 4
    OWN_PRIVATE = 5

    @property
    def uses_global_discovery(self) -> bool:
        """Return whether this mode discovers the global public package set."""

        return self in {self.ALL_PUBLIC, self.ALL_PUBLIC_AND_OWN_PRIVATE}

    @property
    def prepares_snapshot(self) -> bool:
        """Return whether finalization should prepare a database snapshot."""

        return self is not self.CLEAN


@dataclass(frozen=True)
class RunCoordinatorRequest:
    """Stable inputs for one complete application run."""

    today: str
    started_at: int
    mode: int
    github_owner: str
    source_published_today: bool
    working_directory: Path = Path()
    owner_request_limit: int = 100


@dataclass(frozen=True)
class RunCoordinatorExecution:
    """Runtime hooks used for progress and diagnostics."""

    progress: MessageSink
    diagnostic: MessageSink
    now: Clock = lambda: int(time.time())


@dataclass(frozen=True)
class OwnerQueuePhaseRequest:
    """Inputs for the post-discovery global owner queue transition."""

    rest_first: str
    connections_file: Path
    request_limit: int
    include_manual: bool
    working_directory: Path
    now: int


class RunPhaseOperations(Protocol):
    """Cohesive operations sequenced by the top-level coordinator."""

    def prepare_run(self, request: RunCoordinatorRequest) -> RunStartupResult:
        """Prepare persisted state, storage, and package work."""

        raise NotImplementedError

    def discover_owners(
        self,
        today: str,
        skip_explore: bool,
        connections_file: Path,
        packages_all_file: Path,
    ) -> None:
        """Discover global or membership owners into the connection file."""

        raise NotImplementedError

    def prepare_optout_owner_queue(self) -> None:
        """Queue owners affected by a fast opt-out transition."""

        raise NotImplementedError

    def prepare_package_plan(
        self,
        since: str,
        working_directory: Path,
        *,
        reset: bool = False,
    ) -> PackageWorkPlanSummary:
        """Republish package work after a batch transition."""

        raise NotImplementedError

    def prepare_owner_queue(self, request: OwnerQueuePhaseRequest) -> None:
        """Resolve and persist the global owner queue."""

        raise NotImplementedError

    def prepare_targeted_owner_queue(self, connections_file: Path) -> None:
        """Queue the configured owner and discovered memberships."""

        raise NotImplementedError

    def materialize_owner_trees(self, owners: tuple[str, ...]) -> None:
        """Make queued owner paths available in the index workspace."""

        raise NotImplementedError

    def update_owners(self, request: OwnerBatchRequest) -> ExitStatus:
        """Run the queued owner batch."""

        raise NotImplementedError

    def finalize_run(
        self,
        today: str,
        prepare_snapshot: bool,
        working_directory: Path,
    ) -> None:
        """Prepare resumable storage and publish final summaries."""

        raise NotImplementedError


class RunCoordinator:  # pylint: disable=too-few-public-methods
    """Sequence one complete Bkg run around durable phase operations."""

    def __init__(
        self,
        state: StateStore,
        phases: RunPhaseOperations,
        execution: RunCoordinatorExecution,
    ) -> None:
        self.state = state
        self.phases = phases
        self.execution = execution
        self.runtime = BatchRuntimeService(state)
        self._startup_started_at = 0
        self._queue_start_logged = False

    def run(self, request: RunCoordinatorRequest) -> int:
        """Run startup, selected owner work, and finalization in order."""

        mode = RunMode(request.mode)
        self._startup_started_at = request.started_at
        self._queue_start_logged = False
        startup = self.phases.prepare_run(request)
        self._report_package_counts(startup.package_plan)

        run_status = int(ExitStatus.SUCCESS)
        if mode is not RunMode.CLEAN:
            with tempfile.TemporaryDirectory(prefix="bkg-run-") as directory:
                connections_file = Path(directory) / "connections"
                run_status = self._prepare_owner_work(
                    request,
                    startup,
                    connections_file,
                    mode,
                )
                if run_status != ExitStatus.GRACEFUL_STOP:
                    decision = self._update_queued_owners(startup, run_status)
                    if decision.action == "abort":
                        if decision.message:
                            self.execution.diagnostic(decision.message)
                        return decision.run_status
                    run_status = decision.run_status
                    if decision.message:
                        self.execution.progress(decision.message)

        self.phases.finalize_run(
            request.today,
            mode.prepares_snapshot,
            request.working_directory,
        )
        self.state.delete("BKG_TIMEOUT")
        return run_status

    def _prepare_owner_work(
        self,
        request: RunCoordinatorRequest,
        startup: RunStartupResult,
        connections_file: Path,
        mode: RunMode,
    ) -> int:
        if mode.uses_global_discovery:
            if startup.fast_out:
                return self._prepare_fast_optout_queue()
            return self._prepare_global_owner_queue(
                request,
                startup,
                connections_file,
            )
        return self._prepare_targeted_owner_queue(request, connections_file)

    def _prepare_fast_optout_queue(self) -> int:
        self._log_prequeue_elapsed_once()
        status = self._interruptible(self.phases.prepare_optout_owner_queue)
        if status == ExitStatus.GRACEFUL_STOP:
            return int(status)
        return int(ExitStatus.NON_FATAL)

    def _prepare_global_owner_queue(
        self,
        request: RunCoordinatorRequest,
        startup: RunStartupResult,
        connections_file: Path,
    ) -> int:
        skip_explore = (
            request.github_owner == "ipitio"
            and self.runtime.should_skip_daily_gate(
                _EXPLORE_GATE,
                request.today,
                source_published_today=request.source_published_today,
            )
        )
        status = self._interruptible(
            lambda: self.phases.discover_owners(
                request.today,
                skip_explore,
                connections_file,
                request.working_directory / "packages_all",
            )
        )
        if status == ExitStatus.GRACEFUL_STOP:
            self.execution.progress(
                "Reached BKG_MAX_LEN, stopping after persisting state..."
            )
            return int(status)

        transition = self.runtime.complete_batch_if_exhausted(
            request.today,
            startup.package_plan.total,
            startup.package_plan.completed,
        )
        if transition.reset:
            self.phases.prepare_package_plan(
                transition.batch_first_started,
                request.working_directory,
            )

        rest_first = self.state.get("BKG_REST_TO_TOP") or "0"
        self._log_prequeue_elapsed_once()
        phase_started_at = self.execution.now()
        include_manual = not self.runtime.should_skip_daily_gate(
            _OWNER_QUEUE_GATE,
            request.today,
            source_published_today=request.source_published_today,
        )
        if not include_manual:
            self.execution.progress("Skipping owners.txt queue; already ran today")
        status = self._interruptible(
            lambda: self.phases.prepare_owner_queue(
                OwnerQueuePhaseRequest(
                    rest_first,
                    connections_file,
                    request.owner_request_limit,
                    include_manual,
                    request.working_directory,
                    self.execution.now(),
                )
            )
        )
        if include_manual and status != ExitStatus.GRACEFUL_STOP:
            self.runtime.complete_daily_gate(_OWNER_QUEUE_GATE, request.today)
        self._clean_planning_files(request.working_directory)
        self.state.set_many(
            {
                "BKG_DIFF": startup.database_size,
                "BKG_REST_TO_TOP": 1 - int(rest_first),
            }
        )
        self._log_phase("queue-discovered-owners", phase_started_at)
        return int(status)

    def _prepare_targeted_owner_queue(
        self,
        request: RunCoordinatorRequest,
        connections_file: Path,
    ) -> int:
        self._log_prequeue_elapsed_once()
        phase_started_at = self.execution.now()
        status = self._interruptible(
            lambda: self.phases.discover_owners(
                request.today,
                False,
                connections_file,
                request.working_directory / "packages_all",
            )
        )
        if status != ExitStatus.GRACEFUL_STOP:
            status = self._interruptible(
                lambda: self.phases.prepare_targeted_owner_queue(connections_file)
            )
        self._log_phase("queue-membership-owners", phase_started_at)
        return int(status)

    def _update_queued_owners(
        self,
        startup: RunStartupResult,
        run_status: int,
    ) -> OwnerPhaseDecision:
        queued = parse_owner_queue(self.state.get_set("BKG_OWNERS_QUEUE"))
        owner_names = tuple(dict.fromkeys(owner.owner for owner in queued))
        phase_started_at = self.execution.now()
        self.execution.progress(
            f"Materializing {len(owner_names)} queued owner tree(s)..."
        )
        self.phases.materialize_owner_trees(owner_names)
        self._log_phase("materialize-queued-owner-trees", phase_started_at)

        if not queued:
            phase_status = ExitStatus.SUCCESS
        else:
            batch_marker = self.state.get("BKG_BATCH_MARKER")
            if not batch_marker:
                raise ValueError("BKG_BATCH_MARKER is required for owner updates")
            batch_first_started = (
                self.state.get("BKG_BATCH_FIRST_STARTED") or "0000-00-00"
            )
            phase_status = self._interruptible_status(
                lambda: self.phases.update_owners(
                    OwnerBatchRequest(
                        batch_first_started,
                        batch_marker,
                        startup.fast_out,
                    )
                )
            )
        return RunOutcomePolicy.owner_updates(int(phase_status), run_status)

    def _report_package_counts(self, summary: PackageWorkPlanSummary) -> None:
        self.execution.progress(f"all: {summary.total}")
        self.execution.progress(f"done: {summary.completed}")
        self.execution.progress(f"left: {summary.pending}")

    def _log_prequeue_elapsed_once(self) -> None:
        if self._queue_start_logged:
            return
        self._queue_start_logged = True
        self._log_phase("pre-queue-work", self._startup_started_at)

    def _log_phase(self, phase: str, started_at: int) -> None:
        elapsed = max(0, self.execution.now() - started_at)
        self.execution.progress(f"Startup phase '{phase}' completed in {elapsed}s")

    @staticmethod
    def _clean_planning_files(working_directory: Path) -> None:
        for name in _PLANNING_FILES:
            (working_directory / name).unlink(missing_ok=True)

    def _interruptible(self, operation: Callable[[], None]) -> ExitStatus:
        try:
            operation()
        except GracefulStop as error:
            reason = str(error) or "requested"
            self.execution.diagnostic(f"Graceful stop requested: {reason}")
            return ExitStatus.GRACEFUL_STOP
        return ExitStatus.SUCCESS

    def _interruptible_status(
        self,
        operation: Callable[[], ExitStatus],
    ) -> ExitStatus:
        try:
            return operation()
        except GracefulStop as error:
            reason = str(error) or "requested"
            self.execution.diagnostic(f"Graceful stop requested: {reason}")
            return ExitStatus.GRACEFUL_STOP
