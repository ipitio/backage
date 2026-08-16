"""Top-level mode and phase ordering for one bkg run."""

from __future__ import annotations

import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import IntEnum, unique
from pathlib import Path
from typing import Protocol

from ..orchestration import (
    BatchRuntimeService,
    OwnerPhaseDecision,
    owner_updates_decision,
)
from ..owners import (
    OwnerBatchRequest,
    OwnerQueuePreparationResult,
    parse_owner_queue,
)
from ..result import ExitStatus
from ..runtime import GracefulStop
from ..runtime_names import RunFile, StateKey
from ..state import StateStore
from .planning import PackageWorkPlanSummary
from .startup import RunStartupResult

MessageSink = Callable[[str], None]
Clock = Callable[[], int]
_PLANNING_FILES = (
    RunFile.ALL_OWNERS_IN_DB,
    RunFile.OWNERS_PARTIALLY_UPDATED,
    RunFile.OWNERS_STALE,
    RunFile.OWNERS_SCANNED_WITHOUT_PACKAGES,
    RunFile.LEGACY_ALL_OWNERS_TO_UPDATE,
    RunFile.LEGACY_OWNERS_UPDATED,
    RunFile.LEGACY_OWNERS_DEFERRED,
)
_EXPLORE_GATE = StateKey.LAST_EXPLORE_DATE
_OWNER_QUEUE_GATE = StateKey.LAST_OWNERS_QUEUE_DATE


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
class OwnerQueuePhaseRequest:  # pylint: disable=too-many-instance-attributes
    """Inputs for the post-discovery global owner queue transition."""

    rest_first: str
    connections_file: Path
    request_limit: int
    include_manual: bool
    working_directory: Path
    now: int
    batch_marker: str


@dataclass(frozen=True)
class _GlobalOwnerAdmission:
    """One bounded global-owner queue and the request used to continue it."""

    request: OwnerQueuePhaseRequest
    result: OwnerQueuePreparationResult


@dataclass(frozen=True)
class _PreparedOwnerWork:
    """Prepared owner work and optional global-queue continuation state."""

    status: int
    global_admission: _GlobalOwnerAdmission | None = None


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

    def prepare_optout_owner_queue(self, batch_marker: str, now: int) -> None:
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

    def prepare_owner_queue(
        self,
        request: OwnerQueuePhaseRequest,
    ) -> OwnerQueuePreparationResult:
        """Resolve and persist the global owner queue."""

        raise NotImplementedError

    def prepare_targeted_owner_queue(
        self,
        connections_file: Path,
        batch_marker: str,
        now: int,
    ) -> None:
        """Queue the configured owner and discovered memberships."""

        raise NotImplementedError

    def reset_owner_queue(self, batch_marker: str, now: int) -> None:
        """Replace stale generations with one empty active queue."""

        raise NotImplementedError

    def owner_queue_refs(self, batch_marker: str) -> tuple[str, ...]:
        """Return the authoritative remaining owner queue."""

        raise NotImplementedError

    def activate_paused_owner_queue(
        self,
        batch_marker: str,
        now: int,
    ) -> tuple[str, ...]:
        """Make paused owner scans ready for another pass."""

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
    """Sequence one complete bkg run around durable phase operations."""

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
                try:
                    prepared = self._prepare_owner_work(
                        request,
                        startup,
                        connections_file,
                        mode,
                    )
                    run_status = prepared.status
                    if run_status != ExitStatus.GRACEFUL_STOP:
                        decision = self._update_prepared_owner_work(
                            startup,
                            run_status,
                            prepared.global_admission,
                            request.today,
                        )
                        if decision.action == "abort":
                            if decision.message:
                                self.execution.diagnostic(decision.message)
                            return decision.run_status
                        run_status = decision.run_status
                        if decision.message:
                            self.execution.progress(decision.message)
                finally:
                    if mode.uses_global_discovery:
                        self._clean_planning_files(request.working_directory)

        self.phases.finalize_run(
            request.today,
            mode.prepares_snapshot,
            request.working_directory,
        )
        self.state.delete(StateKey.TIMEOUT)
        return run_status

    def _prepare_owner_work(
        self,
        request: RunCoordinatorRequest,
        startup: RunStartupResult,
        connections_file: Path,
        mode: RunMode,
    ) -> _PreparedOwnerWork:
        if mode.uses_global_discovery:
            if startup.fast_out:
                return _PreparedOwnerWork(self._prepare_fast_optout_queue())
            return self._prepare_global_owner_queue(
                request,
                startup,
                connections_file,
            )
        return _PreparedOwnerWork(
            self._prepare_targeted_owner_queue(request, connections_file)
        )

    def _prepare_fast_optout_queue(self) -> int:
        self._log_prequeue_elapsed_once()
        status = self._interruptible(
            lambda: self.phases.prepare_optout_owner_queue(
                self._batch_marker(),
                self.execution.now(),
            )
        )
        if status == ExitStatus.GRACEFUL_STOP:
            return int(status)
        return int(ExitStatus.NON_FATAL)

    def _prepare_global_owner_queue(
        self,
        request: RunCoordinatorRequest,
        startup: RunStartupResult,
        connections_file: Path,
    ) -> _PreparedOwnerWork:
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
                request.working_directory / RunFile.PACKAGES_ALL,
            )
        )
        if status == ExitStatus.GRACEFUL_STOP:
            self.execution.progress(
                "Graceful stop requested; stopping after persisting state..."
            )
            return _PreparedOwnerWork(int(status))

        transition = self.runtime.complete_batch_if_exhausted(
            request.today,
            startup.package_plan.total,
            startup.package_plan.completed,
        )
        if transition.reset:
            self.phases.reset_owner_queue(
                self._batch_marker(),
                self.execution.now(),
            )
            self.phases.prepare_package_plan(
                transition.batch_first_started,
                request.working_directory,
            )

        rest_first = self.state.get(StateKey.REST_TO_TOP) or "0"
        self._log_prequeue_elapsed_once()
        phase_started_at = self.execution.now()
        include_manual = not self.runtime.should_skip_daily_gate(
            _OWNER_QUEUE_GATE,
            request.today,
            source_published_today=request.source_published_today,
        )
        if not include_manual:
            self.execution.progress("Skipping owners.txt queue; already ran today")
        queue_request = OwnerQueuePhaseRequest(
            rest_first=rest_first,
            connections_file=connections_file,
            request_limit=request.owner_request_limit,
            include_manual=include_manual,
            working_directory=request.working_directory,
            now=self.execution.now(),
            batch_marker=self._batch_marker(),
        )
        status, result = self._prepare_owner_queue_interruptibly(queue_request)
        if include_manual and status != ExitStatus.GRACEFUL_STOP:
            self.runtime.complete_daily_gate(_OWNER_QUEUE_GATE, request.today)
        self.state.set_many(
            {
                StateKey.DIFF: startup.database_size,
                StateKey.REST_TO_TOP: 1 - int(rest_first),
            }
        )
        self._log_phase("queue-discovered-owners", phase_started_at)
        admission = (
            _GlobalOwnerAdmission(queue_request, result) if result is not None else None
        )
        return _PreparedOwnerWork(int(status), admission)

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
                request.working_directory / RunFile.PACKAGES_ALL,
            )
        )
        if status != ExitStatus.GRACEFUL_STOP:
            status = self._interruptible(
                lambda: self.phases.prepare_targeted_owner_queue(
                    connections_file,
                    self._batch_marker(),
                    self.execution.now(),
                )
            )
        self._log_phase("queue-membership-owners", phase_started_at)
        return int(status)

    def _update_queued_owners(
        self,
        startup: RunStartupResult,
        run_status: int,
        today: str,
    ) -> OwnerPhaseDecision:
        batch_marker = self._batch_marker()
        queued = parse_owner_queue(self.phases.owner_queue_refs(batch_marker))
        if not queued:
            phase_status = ExitStatus.SUCCESS
        else:
            batch_first_started = (
                self.state.get(StateKey.BATCH_FIRST_STARTED) or "0000-00-00"
            )
            phase_status = self._interruptible_status(
                lambda: self.phases.update_owners(
                    OwnerBatchRequest(
                        since=batch_first_started,
                        batch_marker=batch_marker,
                        today=today,
                        fast_out=startup.fast_out,
                    )
                )
            )
        return owner_updates_decision(int(phase_status), run_status)

    def _update_prepared_owner_work(
        self,
        startup: RunStartupResult,
        run_status: int,
        admission: _GlobalOwnerAdmission | None,
        today: str,
    ) -> OwnerPhaseDecision:
        if admission is None:
            decision = self._update_queued_owners(startup, run_status, today)
            return self._continue_paused_owner_work(startup, decision, today)
        return self._update_global_owner_work(startup, run_status, admission, today)

    def _update_global_owner_work(
        self,
        startup: RunStartupResult,
        run_status: int,
        admission: _GlobalOwnerAdmission,
        today: str,
    ) -> OwnerPhaseDecision:
        current = admission

        while True:
            decision = self._update_queued_owners(startup, run_status, today)
            if decision.action == "abort" or decision.run_status != ExitStatus.SUCCESS:
                return decision
            if not current.result.may_have_more:
                return self._continue_paused_owner_work(startup, decision, today)

            self.execution.progress(
                "Owner queue chunk completed; admitting more pending owners..."
            )
            request = replace(
                current.request,
                now=self.execution.now(),
            )
            status, result = self._prepare_owner_queue_interruptibly(request)
            if status == ExitStatus.GRACEFUL_STOP:
                return owner_updates_decision(int(status), decision.run_status)
            if result is None:
                raise AssertionError("successful owner queue preparation has no result")
            current = _GlobalOwnerAdmission(request, result)

    def _continue_paused_owner_work(
        self,
        startup: RunStartupResult,
        decision: OwnerPhaseDecision,
        today: str,
    ) -> OwnerPhaseDecision:
        while decision.action != "abort" and decision.run_status == ExitStatus.SUCCESS:
            paused = self.phases.activate_paused_owner_queue(
                self._batch_marker(),
                self.execution.now(),
            )
            if not paused:
                return decision
            self.execution.progress(
                f"All available owners received a listing pass; continuing "
                f"{len(paused)} paused owner scan(s)..."
            )
            decision = self._update_queued_owners(
                startup,
                decision.run_status,
                today,
            )
        return decision

    def _batch_marker(self) -> str:
        marker = self.state.get(StateKey.BATCH_MARKER)
        if not marker:
            raise ValueError("BKG_BATCH_MARKER is required for owner updates")
        return marker

    def _prepare_owner_queue_interruptibly(
        self,
        request: OwnerQueuePhaseRequest,
    ) -> tuple[ExitStatus, OwnerQueuePreparationResult | None]:
        try:
            return ExitStatus.SUCCESS, self.phases.prepare_owner_queue(request)
        except GracefulStop as error:
            reason = str(error) or "requested"
            self.execution.diagnostic(f"Graceful stop requested: {reason}")
            return ExitStatus.GRACEFUL_STOP, None

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
