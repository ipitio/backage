"""Construct and share bkg runtime services for one Python operation."""

from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from functools import cached_property
from pathlib import Path

from .concurrency import BoundedWorkerRunner, ConcurrencySettings
from .config import RuntimeConfig, SettingsSnapshot
from .database.composition import DatabaseRepositories
from .database.settings import DatabaseSettings, DatabaseTuning
from .discovery import OwnerIdentityCache, OwnerIdentityResolver
from .github import (
    GitHubClient,
    GitHubRateAccounting,
    GitHubRuntime,
    GitHubSettings,
)
from .github.client import create_http_transport
from .github.user_agent import ChromiumUserAgentResolver
from .owners.lifecycle import (
    OwnerLifecycleExecution,
    OwnerLifecycleService,
    OwnerLifecycleServices,
)
from .owners.operations import (
    OwnerOperationExecution,
    OwnerUpdateOperation,
    OwnerUpdatePolicy,
    OwnerUpdateServices,
)
from .owners.package_updates import (
    OwnerPackageRefreshExecution,
    OwnerPackageRefreshService,
)
from .owners.publication import OwnerPublicationService
from .owners.scan_pages import (
    OwnerScanPageExecution,
    OwnerScanPageService,
)
from .owners.updates import OwnerScanService
from .packages.enrichment import RequestCircuit, RequestCircuitSettings
from .packages.registry.artifacts import (
    ArtifactSizeResolver,
    ContainerArtifactSizeAdapter,
)
from .packages.registry.docker import DockerSizeInspector, DockerSizeSettings
from .packages.registry.ghcr import GHCRBadgeSizeInspector, GHCRManifestInspector
from .packages.registry.graphql import MavenArtifactSizeAdapter
from .packages.registry.sizes import (
    NpmArtifactSizeAdapter,
    NuGetArtifactSizeAdapter,
    PackageRegistryClient,
    RubyGemsArtifactSizeAdapter,
)
from .packages.registry.transport import (
    PackageRegistryTransport,
    PackageRegistryTransportRuntime,
    PackageRegistryTransportSettings,
)
from .packages.updates import PackageRefreshExecution
from .packages.versions.selection import VersionSelectionSettings
from .packages.versions.updates import VersionRefreshExecution
from .publication import PublicationLimits
from .publication.rendering import AggregateSettings
from .runtime import ProcessRunner, StopController
from .snapshots import SnapshotStore
from .state import StateStore

_STOP_BOUND_SERVICES = (
    "database",
    "owner_identity_cache",
    "snapshots",
    "worker_runner",
    "process_runner",
)


@dataclass(frozen=True)
class ApplicationSettings:
    """All core settings derived from one immutable process snapshot."""

    source: SettingsSnapshot = field(repr=False)
    runtime: RuntimeConfig
    github: GitHubSettings
    database_tuning: DatabaseTuning
    aggregate: AggregateSettings
    publication: PublicationLimits

    @classmethod
    def from_env(cls) -> ApplicationSettings:
        """Capture and compose settings at the process boundary."""

        return cls.from_mapping(SettingsSnapshot.from_env())

    @classmethod
    def from_mapping(cls, values: Mapping[str, str]) -> ApplicationSettings:
        """Compose core settings from one supplied mapping."""

        source = (
            values if isinstance(values, SettingsSnapshot) else SettingsSnapshot(values)
        )
        return cls(
            source=source,
            runtime=RuntimeConfig.from_mapping(source),
            github=GitHubSettings.from_mapping(source),
            database_tuning=DatabaseTuning.from_mapping(source),
            aggregate=AggregateSettings.from_mapping(source),
            publication=PublicationLimits.from_mapping(source),
        )

    def as_dict(self) -> dict[str, object]:
        """Return effective non-secret settings for diagnostics."""

        github = asdict(self.github)
        github.pop("token")
        github["token_configured"] = bool(self.github.token)
        database = {
            "path": self.runtime.index_db,
            "owners_table": self.runtime.owners_table,
            "packages_table": self.runtime.packages_table,
            "versions_table": self.runtime.versions_table,
            **asdict(self.database_tuning),
        }
        return {
            **self.runtime.as_dict(),
            "github": github,
            "database": database,
            "aggregate": asdict(self.aggregate),
            "publication": asdict(self.publication),
        }


@dataclass(frozen=True)
class GitHubOperationClients:
    """API and package-registry clients sharing one operation-scoped pool."""

    github: GitHubClient
    package_registry: PackageRegistryTransport


@dataclass
class ApplicationContext:
    """Shared configuration and services for one bkg process."""

    settings: ApplicationSettings
    state: StateStore
    stop: StopController
    metric_enrichment: RequestCircuit = field(init=False, repr=False)
    version_listing_recovery: RequestCircuit = field(init=False, repr=False)
    artifact_size_enrichment: dict[str, RequestCircuit] = field(
        init=False,
        repr=False,
    )
    _lock_diagnostic: Callable[[str], None] | None = field(
        default=None,
        init=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        self._configure_state_locking()
        self._configure_request_circuits()

    @classmethod
    def from_env(cls) -> ApplicationContext:
        """Build the application context from the shell-compatible environment."""

        return cls.from_settings(ApplicationSettings.from_env())

    @classmethod
    def from_mapping(cls, values: Mapping[str, str]) -> ApplicationContext:
        """Build the application context from one supplied settings mapping."""

        return cls.from_settings(ApplicationSettings.from_mapping(values))

    @classmethod
    def from_settings(cls, settings: ApplicationSettings) -> ApplicationContext:
        """Build the application context from composed immutable settings."""

        config = settings.runtime
        state = StateStore(Path(config.env_file))
        return cls(
            settings=settings,
            state=state,
            stop=StopController(state, max_duration=config.max_len),
        )

    @property
    def config(self) -> RuntimeConfig:
        """Return the current run's runtime settings."""

        return self.settings.runtime

    def ensure_state_file(self) -> None:
        """Create the state file when an operation needs to persist values."""

        self.state.path.parent.mkdir(parents=True, exist_ok=True)
        self.state.path.touch(exist_ok=True)

    def configure_run(
        self,
        config: RuntimeConfig,
        *,
        started_at_epoch: float,
    ) -> None:
        """Rebind stop-aware services to one run's final timing configuration."""

        self.settings = replace(self.settings, runtime=config)
        self.stop = StopController(
            self.state,
            max_duration=config.max_len,
            started_at_epoch=started_at_epoch,
        )
        for service in _STOP_BOUND_SERVICES:
            self.__dict__.pop(service, None)
        self._configure_state_locking()
        self._configure_request_circuits()

    def _configure_state_locking(self) -> None:
        self.state.configure_locking(
            check_wait=self.stop.check_lock_wait,
            diagnostic=self._lock_diagnostic,
        )

    def configure_lock_diagnostic(
        self,
        diagnostic: Callable[[str], None],
    ) -> None:
        """Route contended lock diagnostics through serialized run output."""

        self._lock_diagnostic = diagnostic
        self._configure_state_locking()
        self.__dict__.pop("owner_identity_cache", None)

    def _configure_request_circuits(self) -> None:
        self.metric_enrichment = RequestCircuit(check_stop=self.stop.check)
        self.version_listing_recovery = RequestCircuit(
            RequestCircuitSettings(
                max_concurrent=self.config.parallel_async_max_jobs,
            ),
            check_stop=self.stop.check,
        )
        self.artifact_size_enrichment = {
            package_type: RequestCircuit(check_stop=self.stop.check)
            for package_type in ("maven", "npm", "nuget", "rubygems")
        }
        self.artifact_size_enrichment["docker"] = RequestCircuit(
            RequestCircuitSettings(max_concurrent=1),
            check_stop=self.stop.check,
        )

    @cached_property
    def database(self) -> DatabaseRepositories:
        """Return focused repositories configured for this process."""

        return DatabaseRepositories(
            DatabaseSettings.from_config(
                self.config,
                self.settings.database_tuning,
            ),
            check_stop=self.stop.check,
            sleep=self.stop.sleep,
        )

    @property
    def aggregate_settings(self) -> AggregateSettings:
        """Return aggregate settings captured for this process."""

        return self.settings.aggregate

    @property
    def publication_limits(self) -> PublicationLimits:
        """Return publication limits captured for this process."""

        return self.settings.publication

    @cached_property
    def snapshots(self) -> SnapshotStore:
        """Return local snapshot storage configured for this process."""

        return SnapshotStore.from_config(
            self.config,
            check_stop=self.stop.check,
        )

    @property
    def github_settings(self) -> GitHubSettings:
        """Return GitHub settings captured for this process."""

        return self.settings.github

    @cached_property
    def github_rate_accounting(self) -> GitHubRateAccounting:
        """Return application-scoped GitHub REST capacity and usage state."""

        return GitHubRateAccounting(
            self.state,
            rest_reserve=self.github_settings.rest_reserve,
        )

    @cached_property
    def version_selection_settings(self) -> VersionSelectionSettings:
        """Return captured package-version selection limits."""

        return VersionSelectionSettings(
            max_version_pages=self.config.max_version_pages,
            max_tag_pages=self.config.tag_cache_pages,
            append_tagged_limit=self.config.append_tagged_versions_limit,
        )

    @cached_property
    def concurrency_settings(self) -> ConcurrencySettings:
        """Return captured bounded-worker settings for this process."""

        return ConcurrencySettings.from_config(self.config)

    @cached_property
    def worker_runner(self) -> BoundedWorkerRunner:
        """Return the shared bounded-worker policy for Python-owned loops."""

        return BoundedWorkerRunner(
            self.concurrency_settings,
            check_stop=self.stop.check,
        )

    @cached_property
    def process_runner(self) -> ProcessRunner:
        """Return the stop-aware external process runner."""

        return ProcessRunner(self.stop)

    @cached_property
    def owner_identity_cache(self) -> OwnerIdentityCache:
        """Return the shared stop-aware owner identity cache."""

        return OwnerIdentityCache.from_config(
            self.config,
            check_lock_wait=self.stop.check_lock_wait,
            lock_diagnostic=self._lock_diagnostic,
        )

    def owner_update_operation(
        self,
        client: GitHubClient,
        package_registry: PackageRegistryClient,
        execution: OwnerOperationExecution,
    ) -> OwnerUpdateOperation:
        """Compose one owner updater from application-owned concrete services."""

        return _owner_update_operation(self, client, package_registry, execution)

    @contextmanager
    def github_client(
        self,
        *,
        report: Callable[[str], None] | None = None,
    ) -> Generator[GitHubClient]:
        """Yield a pooled client connected to this process's state and stop control."""

        with github_operation_clients(self, report=report) as clients:
            yield clients.github


@contextmanager
def github_operation_clients(
    application: ApplicationContext,
    *,
    report: Callable[[str], None] | None = None,
) -> Generator[GitHubOperationClients]:
    """Yield API and registry clients over one application-owned HTTP pool."""

    application.ensure_state_file()
    runtime = GitHubRuntime(
        check_stop=application.stop.check,
        request_stop=application.stop.request_stop,
        sleep=application.stop.sleep,
        wall_clock=application.stop.timing.wall_clock,
        report=report or (lambda _message: None),
    )
    with create_http_transport(application.github_settings) as transport:
        user_agent = ChromiumUserAgentResolver(
            transport,
            override=application.github_settings.user_agent_override,
            check_stop=runtime.check_stop,
            report=runtime.report,
        ).resolve
        package_registry = PackageRegistryTransport(
            transport,
            PackageRegistryTransportSettings(
                token=application.github_settings.token,
                user_agent=user_agent,
                connect_timeout=application.github_settings.connect_timeout,
                read_timeout=application.github_settings.read_timeout,
                write_timeout=application.github_settings.write_timeout,
                pool_timeout=application.github_settings.pool_timeout,
            ),
            PackageRegistryTransportRuntime(
                check_stop=runtime.check_stop,
                clock=runtime.clock,
            ),
        )
        with GitHubClient(
            application.github_settings,
            accounting=application.github_rate_accounting,
            runtime=runtime,
            client=transport,
            user_agent=user_agent,
        ) as client:
            yield GitHubOperationClients(client, package_registry)


def _owner_update_operation(
    application: ApplicationContext,
    client: GitHubClient,
    package_registry: PackageRegistryClient,
    execution: OwnerOperationExecution,
) -> OwnerUpdateOperation:
    index_dir = application.config.index_dir
    if index_dir is None:
        raise ValueError("BKG_INDEX_DIR is required")
    identity = OwnerIdentityResolver(application.owner_identity_cache, client)
    artifact_sizes = _artifact_size_resolver(
        application,
        client,
        package_registry,
        execution.diagnostic,
    )
    return OwnerUpdateOperation(
        OwnerUpdateServices(
            application.database.owner_identities,
            application.database.owners,
            application.state,
            identity,
            lambda today: _owner_lifecycle(
                application,
                client,
                execution,
                artifact_sizes,
                today,
            ),
        ),
        OwnerUpdatePolicy(
            mode=application.config.mode,
            versions_table=application.config.versions_table,
            index_dir=Path(index_dir),
            use_rest_api=bool(application.github_settings.token),
        ),
        execution,
    )


def _owner_lifecycle(
    application: ApplicationContext,
    client: GitHubClient,
    execution: OwnerOperationExecution,
    artifact_sizes: ArtifactSizeResolver,
    today: str,
) -> OwnerLifecycleService:
    package_refresh = _package_refresh_service(
        application,
        client,
        execution,
        artifact_sizes,
        today,
    )
    pages = OwnerScanPageService(
        application.database.owners,
        client,
        package_refresh,
        OwnerScanPageExecution(
            application.stop.check,
            execution.progress,
        ),
    )
    return OwnerLifecycleService(
        application.database.owners,
        OwnerLifecycleServices(
            package_refresh,
            OwnerScanService(
                application.database.owners,
                client,
                pages,
                package_refresh,
            ),
            OwnerPublicationService(
                application.database.packages,
                application.aggregate_settings,
                application.publication_limits,
                application.stop.check,
            ),
        ),
        OwnerLifecycleExecution(
            application.state,
            execution.progress,
        ),
    )


def _package_refresh_service(
    application: ApplicationContext,
    client: GitHubClient,
    execution: OwnerOperationExecution,
    artifact_sizes: ArtifactSizeResolver,
    today: str,
) -> OwnerPackageRefreshService:
    return OwnerPackageRefreshService(
        application.database.packages,
        client,
        OwnerPackageRefreshExecution(
            PackageRefreshExecution(
                VersionRefreshExecution(
                    BoundedWorkerRunner(
                        execution.concurrency,
                        check_stop=application.stop.check,
                    ),
                    artifact_sizes,
                    diagnostic=execution.diagnostic,
                    today=lambda: today,
                    metric_enrichment=application.metric_enrichment,
                    listing_recovery=application.version_listing_recovery,
                ),
                application.version_selection_settings,
                application.publication_limits,
                Path(application.config.optout_file),
                application.stop.check,
            ),
            execution.concurrency,
            execution.progress,
            execution.diagnostic,
        ),
    )


def _artifact_size_resolver(
    application: ApplicationContext,
    client: GitHubClient,
    package_registry: PackageRegistryClient,
    diagnostic: Callable[[str], None],
) -> ArtifactSizeResolver:
    return ArtifactSizeResolver(
        {
            "container": ContainerArtifactSizeAdapter(
                manifest_inspector=GHCRManifestInspector(
                    client,
                    diagnostic=diagnostic,
                ),
                hosted_inspector=GHCRBadgeSizeInspector(
                    client,
                    application.metric_enrichment,
                    diagnostic=diagnostic,
                ),
                diagnostic=diagnostic,
                local_inspector=DockerSizeInspector(
                    application.process_runner,
                    application.artifact_size_enrichment["docker"],
                    DockerSizeSettings(
                        enabled=application.config.docker_size_fallback,
                        platform=application.config.docker_platform,
                        pull_timeout=application.config.docker_pull_timeout,
                        command_timeout=application.config.docker_command_timeout,
                    ),
                    diagnostic=diagnostic,
                ),
            ),
            "maven": MavenArtifactSizeAdapter(
                client,
                application.artifact_size_enrichment["maven"],
                diagnostic=diagnostic,
            ),
            "npm": NpmArtifactSizeAdapter(
                package_registry,
                application.artifact_size_enrichment["npm"],
                diagnostic=diagnostic,
            ),
            "nuget": NuGetArtifactSizeAdapter(
                package_registry,
                application.artifact_size_enrichment["nuget"],
                diagnostic=diagnostic,
            ),
            "rubygems": RubyGemsArtifactSizeAdapter(
                package_registry,
                application.artifact_size_enrichment["rubygems"],
                diagnostic=diagnostic,
            ),
        }
    )
