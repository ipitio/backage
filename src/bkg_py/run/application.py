"""Compose existing application services into top-level run phases."""

from __future__ import annotations

from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import Lock

from ..application import ApplicationContext
from ..discovery import DiscoveryError, OwnerIdentityCache, OwnerIdentityResolver
from ..discovery_fallback import PublicHtmlDiscoveryTraversal
from ..discovery_operations import (
    DiscoveryPhaseExecution,
    DiscoveryPhaseIdentity,
    DiscoveryPhasePaths,
    DiscoveryPhaseRequest,
    DiscoveryPhaseService,
    DiscoveryPhaseServices,
    DiscoveryTraversal,
)
from ..github import GitHubClient, GitHubError
from ..orchestration import BatchRuntimeService
from ..owner_batch import (
    OwnerBatchEffects,
    OwnerBatchExecution,
    OwnerBatchRequest,
    OwnerBatchService,
)
from ..owner_operations import OwnerOperationExecution, OwnerUpdateOperation
from ..owner_pages import OwnerPageAdmissionConfig, admit_owner_page
from ..owner_queue_operations import (
    OwnerQueuePreparationExecution,
    OwnerQueuePreparationPaths,
    OwnerQueuePreparationRequest,
    OwnerQueuePreparationService,
    OwnerQueuePreparationServices,
    TargetedOwnerQueueService,
)
from ..result import ExitStatus
from ..run_finalization import (
    RunFinalizationExecution,
    RunFinalizationRequest,
    RunFinalizationService,
    RunFinalizationServices,
)
from ..run_planning import PackageWorkPlanService, PackageWorkPlanSummary
from ..run_publication import (
    RunPublicationIdentity,
    RunPublicationPaths,
    RunPublicationRequest,
    RunPublicationService,
)
from ..run_startup import (
    RunStartupExecution,
    RunStartupRequest,
    RunStartupResult,
    RunStartupService,
    RunStartupServices,
)
from .coordinator import OwnerQueuePhaseRequest, RunCoordinatorRequest

MessageSink = Callable[[str], None]
OwnerMaterializer = Callable[[tuple[str, ...]], None]


@dataclass(frozen=True)
class RunApplicationExecution:
    """Runtime output and workspace hooks for concrete run phases."""

    progress: MessageSink
    diagnostic: MessageSink
    materialize_owner_trees: OwnerMaterializer


class RunApplicationOperations:
    """Adapt existing services to the top-level coordinator's phase surface."""

    def __init__(
        self,
        application: ApplicationContext,
        execution: RunApplicationExecution,
        *,
        github_client: GitHubClient | None = None,
    ) -> None:
        self.application = application
        self.execution = execution
        self._shared_github_client = github_client

    def prepare_run(self, request: RunCoordinatorRequest) -> RunStartupResult:
        """Prepare persisted state, storage, and the initial package plan."""

        config = self.application.config
        if config.index_db is None:
            raise ValueError("BKG_INDEX_DB is required")
        return RunStartupService(
            RunStartupServices(
                self.application.database,
                self.application.snapshots,
                self.application.state,
                OwnerIdentityCache.from_config(config),
            ),
            RunStartupExecution(
                self.application.stop.check,
                self.execution.progress,
            ),
        ).prepare(
            RunStartupRequest(
                request.today,
                request.started_at,
                request.working_directory,
                Path(config.index_db),
                Path(config.optout_file),
                config.github_owner,
            )
        )

    def discover_owners(
        self,
        today: str,
        skip_explore: bool,
        connections_file: Path,
        packages_all_file: Path,
    ) -> None:
        """Run authenticated discovery with the existing public fallback."""

        config = self.application.config
        with self._github_client() as client:
            resolver = OwnerIdentityResolver(
                OwnerIdentityCache.from_config(config),
                client,
            )
            admission = OwnerPageAdmissionConfig(
                self.application.state,
                Path(config.owners_file),
                packages_all_file,
            )
            request = DiscoveryPhaseRequest(
                paths=DiscoveryPhasePaths(
                    connections_file,
                    Path(config.owners_file),
                    Path(config.optout_file),
                ),
                identity=DiscoveryPhaseIdentity(
                    config.github_owner,
                    config.github_repo,
                    config.github_owner == "ipitio" and config.mode in {0, 3},
                ),
                today=today,
                skip_explore=skip_explore,
                first_run=config.is_first != "false" and config.mode in {0, 3},
                owner_page_limit=config.owner_discovery_max_pages,
            )
            if self.application.github_settings.token:
                try:
                    self._run_discovery(resolver, resolver, admission, request)
                    return
                except (DiscoveryError, GitHubError) as error:
                    self.execution.progress(
                        f"Authenticated discovery failed: {error}; "
                        "using public HTML fallback"
                    )
            else:
                self.execution.progress(
                    "GITHUB_TOKEN unavailable; using public HTML discovery"
                )

            fallback = PublicHtmlDiscoveryTraversal(
                client,
                diagnostic=self.execution.progress,
            )
            owner_page_limit = (
                request.owner_page_limit
                if self.application.github_settings.token
                else 0
            )
            self._run_discovery(
                resolver,
                fallback,
                admission,
                DiscoveryPhaseRequest(
                    paths=request.paths,
                    identity=request.identity,
                    today=request.today,
                    skip_explore=request.skip_explore,
                    first_run=request.first_run,
                    owner_page_limit=owner_page_limit,
                ),
            )

    def prepare_optout_owner_queue(self) -> None:
        """Resolve and queue owners named by opt-out entries."""

        with self._github_client() as client:
            self._targeted_queue_service(client).prepare_optouts(
                Path(self.application.config.optout_file)
            )

    def prepare_package_plan(
        self,
        since: str,
        working_directory: Path,
        *,
        reset: bool = False,
    ) -> PackageWorkPlanSummary:
        """Publish the current package plan after a batch transition."""

        return PackageWorkPlanService(self.application.database).prepare(
            since,
            working_directory,
            batch_marker=self.application.state.get("BKG_BATCH_MARKER") or "",
            reset=reset,
        )

    def prepare_owner_queue(self, request: OwnerQueuePhaseRequest) -> None:
        """Resolve discovered candidates and persist the global owner queue."""

        config = self.application.config
        if config.index_dir is None:
            raise ValueError("BKG_INDEX_DIR is required")
        effects = OwnerBatchEffects(
            self.application.database,
            self.application.state,
            Path(config.owners_file),
            Path(config.index_dir),
            self.execution.progress,
        )
        with self._github_client() as client:
            OwnerQueuePreparationService(
                OwnerQueuePreparationServices(
                    self.application.database,
                    OwnerIdentityResolver(
                        OwnerIdentityCache.from_config(config),
                        client,
                    ),
                    self.application.state,
                    effects.retire_unavailable,
                ),
                OwnerQueuePreparationExecution(
                    self.application.stop.check,
                    self.execution.progress,
                ),
            ).prepare(
                OwnerQueuePreparationRequest(
                    paths=OwnerQueuePreparationPaths(
                        connections=request.connections_file,
                        manual_owners=Path(config.owners_file),
                        index_directory=Path(config.index_dir),
                        working_directory=request.working_directory,
                    ),
                    rest_first=request.rest_first,
                    request_limit=request.request_limit,
                    current_owner=config.github_owner,
                    include_manual=request.include_manual,
                    now=request.now,
                )
            )

    def prepare_targeted_owner_queue(self, connections_file: Path) -> None:
        """Queue the configured owner and discovered memberships."""

        with self._github_client() as client:
            self._targeted_queue_service(client).prepare(
                self.application.config.github_owner,
                connections_file,
            )

    def materialize_owner_trees(self, owners: tuple[str, ...]) -> None:
        """Delegate index workspace materialization to its current owner."""

        self.execution.materialize_owner_trees(owners)

    def update_owners(self, request: OwnerBatchRequest) -> ExitStatus:
        """Run the complete owner queue with one shared GitHub client."""

        config = self.application.config
        if config.index_dir is None:
            raise ValueError("BKG_INDEX_DIR is required")
        with self._github_client() as client:
            service = OwnerBatchService(
                lambda concurrency: (
                    OwnerUpdateOperation(
                        self.application,
                        client,
                        OwnerOperationExecution(
                            concurrency,
                            self.execution.progress,
                            self.execution.diagnostic,
                        ),
                    ).update
                ),
                OwnerBatchEffects(
                    self.application.database,
                    self.application.state,
                    Path(config.owners_file),
                    Path(config.index_dir),
                    self.execution.progress,
                ),
                OwnerBatchExecution(
                    self.application.state,
                    Path(config.optout_file),
                    self.application.concurrency_settings,
                    self.application.stop.check,
                    self.execution.progress,
                    self.execution.diagnostic,
                ),
            )
            return service.run(request)

    def finalize_run(
        self,
        today: str,
        prepare_snapshot: bool,
        working_directory: Path,
    ) -> None:
        """Finalize storage and summaries after deferring an existing stop."""

        config = self.application.config
        with self.application.stop.finalization_scope():
            RunFinalizationService(
                RunFinalizationServices(
                    self.application.database,
                    self.application.snapshots,
                    RunPublicationService(
                        self.application.database,
                        self.application.state,
                        self.application.stop.check,
                    ),
                    self.application.state,
                ),
                RunFinalizationExecution(
                    self.application.stop.check,
                    self.execution.progress,
                ),
            ).finalize(
                RunFinalizationRequest(
                    publication=self._publication_request(today, working_directory),
                    optout_file=Path(config.optout_file),
                    batch_first_started=(
                        self.application.state.get("BKG_BATCH_FIRST_STARTED") or today
                    ),
                    prepare_snapshot=prepare_snapshot,
                    rotation_threshold_bytes=(config.snapshot_rotation_threshold_bytes),
                )
            )

    def _run_discovery(
        self,
        resolver: OwnerIdentityResolver,
        traversal: DiscoveryTraversal,
        admission: OwnerPageAdmissionConfig,
        request: DiscoveryPhaseRequest,
    ) -> None:
        service = DiscoveryPhaseService(
            DiscoveryPhaseServices(
                traversal,
                lambda page, per_page: admit_owner_page(
                    resolver,
                    admission,
                    page,
                    per_page,
                ),
                self._complete_explore_gate,
            ),
            DiscoveryPhaseExecution(
                self.application.stop.check,
                self.execution.progress,
            ),
        )
        service.run(request)

    def _complete_explore_gate(self, today: str) -> None:
        BatchRuntimeService(self.application.state).complete_daily_gate(
            "BKG_LAST_EXPLORE_DATE",
            today,
        )

    def _targeted_queue_service(
        self,
        client: GitHubClient,
    ) -> TargetedOwnerQueueService:
        return TargetedOwnerQueueService(
            OwnerIdentityResolver(
                OwnerIdentityCache.from_config(self.application.config),
                client,
            ),
            self.application.state,
            self.application.stop.check,
            self.execution.progress,
        )

    def _publication_request(
        self,
        today: str,
        working_directory: Path,
    ) -> RunPublicationRequest:
        config = self.application.config
        if config.index_dir is None:
            raise ValueError("BKG_INDEX_DIR is required")
        if config.github_branch is None:
            raise ValueError("GITHUB_BRANCH is required")
        return RunPublicationRequest(
            paths=RunPublicationPaths(
                root=Path(config.root),
                index_directory=Path(config.index_dir),
                working_directory=working_directory,
            ),
            identity=RunPublicationIdentity(
                github_owner=config.github_owner,
                github_repo=config.github_repo,
                github_branch=config.github_branch,
            ),
            today=today,
            rotated=False,
        )

    @contextmanager
    def _github_client(self) -> Generator[GitHubClient, None, None]:
        if self._shared_github_client is not None:
            yield self._shared_github_client
            return
        with self.application.github_client() as client:
            yield client


class LockedRunOutput:
    """Serialize progress from concurrent owner workers."""

    def __init__(self, progress: MessageSink, diagnostic: MessageSink) -> None:
        self._progress = progress
        self._diagnostic = diagnostic
        self._lock = Lock()

    def progress(self, message: str) -> None:
        """Write one progress message under the output lock."""

        with self._lock:
            self._progress(message)

    def diagnostic(self, message: str) -> None:
        """Write one diagnostic message under the output lock."""

        with self._lock:
            self._diagnostic(message)
