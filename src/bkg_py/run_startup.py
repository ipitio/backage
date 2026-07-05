"""Prepare durable run state and package work before discovery."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .database import DatabaseRepository
from .discovery import OwnerIdentityCache
from .files import atomic_text_output
from .orchestration import BatchRuntimeService
from .owner_queue import normalize_owner_lines
from .run_planning import PackageWorkPlanService, PackageWorkPlanSummary
from .snapshots import SnapshotError, SnapshotStore
from .state import StateStore

MessageSink = Callable[[str], None]
StopCheck = Callable[[], None]
Clock = Callable[[], int]


@dataclass(frozen=True)
class RunStartupRequest:
    """Filesystem and runtime inputs for one application startup."""

    today: str
    started_at: int
    working_directory: Path
    database_path: Path
    optout_file: Path
    github_owner: str


@dataclass(frozen=True)
class RunStartupResult:
    """Startup values consumed by the compatibility launcher."""

    batch_first_started: str
    package_plan: PackageWorkPlanSummary
    database_size: int
    opted_out: int
    fast_out: bool


@dataclass(frozen=True)
class RunStartupServices:
    """Stateful services participating in application startup."""

    repository: DatabaseRepository
    snapshots: SnapshotStore
    state: StateStore
    identity_cache: OwnerIdentityCache


@dataclass(frozen=True)
class RunStartupExecution:
    """Runtime hooks used while preparing one application run."""

    check_stop: StopCheck
    progress: MessageSink
    now: Clock = lambda: int(time.time())


class RunStartupService:  # pylint: disable=too-few-public-methods
    """Initialize one run and publish its package-work snapshot."""

    def __init__(
        self,
        services: RunStartupServices,
        execution: RunStartupExecution,
    ) -> None:
        self.services = services
        self.execution = execution

    def prepare(self, request: RunStartupRequest) -> RunStartupResult:
        """Prepare state, storage, and current package work in order."""

        self.services.state.path.parent.mkdir(parents=True, exist_ok=True)
        self.services.state.path.touch(exist_ok=True)
        initialized = BatchRuntimeService(self.services.state).begin_run(
            request.today,
            request.started_at,
        )
        self.services.identity_cache.reset()
        self._restore_snapshot()

        phase_started_at = self.execution.now()
        self._recover_database_backup(request.database_path)
        self.services.repository.ensure_schema()
        progress_marker = self.services.state.get("BKG_PACKAGE_PROGRESS_MARKER")
        if progress_marker != initialized.batch_marker:
            if progress_marker is None:
                self.services.repository.bootstrap_package_batch(
                    initialized.batch_marker,
                    initialized.batch_first_started,
                )
            self.services.state.set(
                "BKG_PACKAGE_PROGRESS_MARKER",
                initialized.batch_marker,
            )
        summary = PackageWorkPlanService(self.services.repository).prepare(
            initialized.batch_first_started,
            request.working_directory,
            batch_marker=initialized.batch_marker,
        )
        opted_out = _normalize_owner_file(request.optout_file)
        previous_opted_out = self.services.state.get("BKG_OUT")
        fast_out = bool(
            request.github_owner == "ipitio"
            and previous_opted_out is not None
            and self.services.state.get_int("BKG_OUT") < opted_out
        )
        database_size = request.database_path.stat().st_size
        self._log_phase("prepare-package-state", phase_started_at)
        return RunStartupResult(
            initialized.batch_first_started,
            summary,
            database_size,
            opted_out,
            fast_out,
        )

    def _restore_snapshot(self) -> None:
        phase_started_at = self.execution.now()
        try:
            result = self.services.snapshots.restore_database_if_needed()
        except (OSError, SnapshotError) as error:
            self.execution.progress(f"Database snapshot restore skipped: {error}")
            result = None
        if result is None:
            return
        self.execution.progress(result.message)
        self._log_phase("restore-db-from-snapshot", phase_started_at)

    def _recover_database_backup(self, database_path: Path) -> None:
        self.execution.check_stop()
        if database_path.is_file():
            return
        database_path.parent.mkdir(parents=True, exist_ok=True)
        backup = Path(f"{database_path}.bak")
        if backup.is_file():
            backup.replace(database_path)

    def _log_phase(self, phase: str, started_at: int) -> None:
        elapsed = max(0, self.execution.now() - started_at)
        self.execution.progress(f"Startup phase '{phase}' completed in {elapsed}s")


def _normalize_owner_file(path: Path) -> int:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []
    owners = normalize_owner_lines(lines)
    path.parent.mkdir(parents=True, exist_ok=True)
    with atomic_text_output(path) as output:
        if owners:
            output.write("\n".join(owners))
            output.write("\n")
    return len(owners)
