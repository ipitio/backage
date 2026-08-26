"""Prepare durable run state and package work before discovery."""

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..database.catalog.package_repository import PackageCatalogRepository
from ..database.history.package_history import (
    MIGRATION_BATCH_ROWS as PACKAGE_MIGRATION_ROWS,
)
from ..database.history.repository import HistoryRepository
from ..database.history.version_history import (
    MIGRATION_BATCH_ROWS as VERSION_MIGRATION_ROWS,
)
from ..database.models import PackageCatalogStatus
from ..database.owner.queue_repository import OwnerQueueRepository
from ..database.package.repository import PackageRepository
from ..discovery import OwnerIdentityCache
from ..discovery.values import normalize_owner_lines
from ..files import atomic_text_output
from ..orchestration import BatchRuntimeService
from ..runtime import peak_resident_memory_mib
from ..runtime_names import StateKey
from ..snapshots import SnapshotError, SnapshotStore
from ..state import StateStore
from ..workspace import WorkspaceError, read_index_package_catalog
from .planning import PackageWorkPlanService, PackageWorkPlanSummary

MessageSink = Callable[[str], None]
StopCheck = Callable[[], None]
Clock = Callable[[], int]
_HISTORY_MIGRATION_BUDGET_SECONDS = 60.0


@dataclass(frozen=True)
class RunStartupRequest:
    """Filesystem and runtime inputs for one application startup."""

    today: str
    started_at: int
    working_directory: Path
    database_path: Path
    optout_file: Path
    github_owner: str
    index_directory: Path | None = None


@dataclass(frozen=True)
class RunStartupResult:
    """Startup values consumed by the run coordinator."""

    batch_first_started: str
    package_plan: PackageWorkPlanSummary
    database_size: int
    opted_out: int
    fast_out: bool


@dataclass(frozen=True)
class RunStartupServices:
    """Stateful services participating in application startup."""

    packages: PackageRepository
    catalog: PackageCatalogRepository
    history: HistoryRepository
    owner_queue: OwnerQueueRepository
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
        legacy_owner_queue = tuple(
            self.services.state.get_set(StateKey.LEGACY_OWNERS_QUEUE)
        )
        initialized = BatchRuntimeService(self.services.state).begin_run(
            request.today,
            request.started_at,
        )
        self.services.identity_cache.reset()
        self._restore_snapshot()

        phase_started_at = self.execution.now()
        self._recover_database_backup(request.database_path)
        self.services.packages.ensure_schema()
        self._migrate_history()
        self._prepare_package_catalog(request)
        self._recover_owner_queue(
            initialized.batch_marker,
            legacy_owner_queue,
            request.started_at,
        )
        progress_marker = self.services.state.get(StateKey.PACKAGE_PROGRESS_MARKER)
        if progress_marker != initialized.batch_marker:
            if progress_marker is None:
                self.services.packages.bootstrap_package_batch(
                    initialized.batch_marker,
                    initialized.batch_first_started,
                )
            self.services.state.set(
                StateKey.PACKAGE_PROGRESS_MARKER,
                initialized.batch_marker,
            )
        summary = PackageWorkPlanService(self.services.packages).prepare(
            initialized.batch_first_started,
            request.working_directory,
            batch_marker=initialized.batch_marker,
        )
        opted_out = _normalize_owner_file(request.optout_file)
        previous_opted_out = self.services.state.get(StateKey.OUT)
        fast_out = bool(
            request.github_owner == "ipitio"
            and previous_opted_out is not None
            and self.services.state.get_int(StateKey.OUT) < opted_out
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

    def _prepare_package_catalog(self, request: RunStartupRequest) -> None:
        previous = self.services.catalog.package_catalog_status()
        if request.index_directory is None:
            if previous is not None:
                self._report_package_catalog("ready", previous)
            return

        started_at = time.monotonic()
        try:
            self.execution.check_stop()
            tree = read_index_package_catalog(
                request.index_directory,
                None if previous is None else previous.source_revision,
            )
            if previous is not None and previous.source_revision == tree.revision:
                self._report_package_catalog("ready", previous)
                return
            self.execution.check_stop()
        except WorkspaceError as error:
            operation = "initialization" if previous is None else "synchronization"
            retained = (
                ""
                if previous is None
                else f"; retaining {previous.inventory.packages} ready package paths"
            )
            self.execution.progress(
                f"Package catalog {operation} deferred: {error}{retained}"
            )
            return

        catalog_status = self.services.catalog.initialize_package_catalog(
            tree.paths,
            tree.revision,
            request.today,
        )
        elapsed = max(0.0, time.monotonic() - started_at)
        operation = "initialized" if previous is None else "synchronized"
        self._report_package_catalog(operation, catalog_status, elapsed=elapsed)

    def _migrate_history(self) -> None:
        started_at = time.monotonic()
        totals = {"Version": 0, "Package": 0}
        remaining = {"Version": 0, "Package": 0}
        complete = {"Version": False, "Package": False}
        migrations = (
            (
                "Version",
                self.services.history.migrate_version_history,
                VERSION_MIGRATION_ROWS,
            ),
            (
                "Package",
                self.services.history.migrate_package_history,
                PACKAGE_MIGRATION_ROWS,
            ),
        )
        while True:
            moved_rows = 0
            for name, migrate, row_limit in migrations:
                if complete[name]:
                    continue
                self.execution.check_stop()
                progress = migrate(row_limit)
                totals[name] += progress.migrated_rows
                remaining[name] = progress.remaining_rows
                complete[name] = progress.complete
                moved_rows += progress.migrated_rows
                if time.monotonic() - started_at >= _HISTORY_MIGRATION_BUDGET_SECONDS:
                    break
            elapsed = time.monotonic() - started_at
            if (
                all(complete.values())
                or moved_rows == 0
                or elapsed >= _HISTORY_MIGRATION_BUDGET_SECONDS
            ):
                break
        peak_rss = peak_resident_memory_mib()
        for name, _, _ in migrations:
            if totals[name] == 0 and remaining[name] == 0:
                continue
            self.execution.progress(
                f"{name} history migration: "
                f"migrated={totals[name]} remaining={remaining[name]} "
                f"complete={int(remaining[name] == 0)} elapsed={elapsed:.3f}s "
                f"peak_rss={peak_rss:.1f}MiB"
            )

    def _report_package_catalog(
        self,
        operation: str,
        catalog_status: PackageCatalogStatus,
        *,
        elapsed: float | None = None,
    ) -> None:
        timing = "" if elapsed is None else f"; elapsed={elapsed:.3f}s"
        self.execution.progress(
            f"Package catalog {operation}: "
            f"owners={catalog_status.inventory.owners} "
            f"repositories={catalog_status.inventory.repositories} "
            f"packages={catalog_status.inventory.packages} "
            f"resolved={catalog_status.resolved_packages} "
            f"source={catalog_status.source_revision[:12]}{timing}"
        )

    def _recover_owner_queue(
        self,
        batch_marker: str,
        legacy_owner_queue: tuple[str, ...],
        started_at: int,
    ) -> None:
        queue_before = self.services.owner_queue.owner_queue_stats(batch_marker)
        recovery_started_at = time.monotonic()
        self.services.owner_queue.prepare_owner_queue(
            batch_marker,
            legacy_owner_queue,
            started_at,
        )
        legacy_removed = self.services.state.delete(StateKey.LEGACY_OWNERS_QUEUE)
        recovery_elapsed = max(0.0, time.monotonic() - recovery_started_at)
        queue_after = self.services.owner_queue.owner_queue_stats(batch_marker)
        self.execution.progress(
            "Owner queue recovery: "
            f"active={queue_after.total} ready={queue_after.ready} "
            f"claimed={queue_after.claimed} paused={queue_after.paused} "
            f"completed={queue_after.completed} "
            f"candidates={queue_after.candidates} "
            f"imported={max(0, queue_after.total - queue_before.total)} "
            f"legacy_removed={int(legacy_removed)} "
            f"recovered_claims={queue_before.claimed} "
            f"pruned_stale={queue_before.stale_rows + queue_before.stale_candidates}; "
            f"sqlite={recovery_elapsed:.3f}s "
            f"peak_rss={peak_resident_memory_mib():.1f}MiB"
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
