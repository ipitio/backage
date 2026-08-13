"""Construct and share bkg runtime services for one Python operation."""

from __future__ import annotations

from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from functools import cached_property
from pathlib import Path

from .concurrency import BoundedWorkerRunner, ConcurrencySettings
from .config import RuntimeConfig, SettingsSnapshot
from .database import DatabaseRepository
from .database.settings import DatabaseSettings, DatabaseTuning
from .discovery import OwnerIdentityCache
from .enrichment import RequestCircuit, RequestCircuitSettings
from .github import (
    GitHubClient,
    GitHubRateAccounting,
    GitHubRuntime,
    GitHubSettings,
)
from .publication import PublicationLimits
from .rendering import AggregateSettings
from .runtime import ProcessRunner, StopController
from .snapshots import SnapshotStore
from .state import StateStore
from .version_selection import VersionSelectionSettings

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
    def database(self) -> DatabaseRepository:
        """Return one repository configured for this process."""

        return DatabaseRepository(
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

    @contextmanager
    def github_client(
        self,
        *,
        report: Callable[[str], None] | None = None,
    ) -> Generator[GitHubClient]:
        """Yield a pooled client connected to this process's state and stop control."""

        self.ensure_state_file()
        with GitHubClient(
            self.github_settings,
            accounting=self.github_rate_accounting,
            runtime=GitHubRuntime(
                check_stop=self.stop.check,
                request_stop=self.stop.request_stop,
                sleep=self.stop.sleep,
                wall_clock=self.stop.timing.wall_clock,
                report=report or (lambda _message: None),
            ),
        ) as client:
            yield client
