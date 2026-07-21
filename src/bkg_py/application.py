"""Construct and share bkg runtime services for one Python operation."""

from __future__ import annotations

from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path

from .concurrency import BoundedWorkerRunner, ConcurrencySettings
from .config import RuntimeConfig
from .database import DatabaseRepository, DatabaseSettings
from .enrichment import MetricEnrichmentCircuit
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

_STOP_BOUND_SERVICES = ("database", "snapshots", "worker_runner", "process_runner")


@dataclass
class ApplicationContext:
    """Shared configuration and services for one bkg process."""

    config: RuntimeConfig
    state: StateStore
    stop: StopController
    metric_enrichment: MetricEnrichmentCircuit = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.metric_enrichment = MetricEnrichmentCircuit(check_stop=self.stop.check)

    @classmethod
    def from_env(cls) -> ApplicationContext:
        """Build the application context from the shell-compatible environment."""

        config = RuntimeConfig.from_env()
        state = StateStore(Path(config.env_file))
        return cls(
            config=config,
            state=state,
            stop=StopController(state, max_duration=config.max_len),
        )

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

        self.config = config
        self.stop = StopController(
            self.state,
            max_duration=config.max_len,
            started_at_epoch=started_at_epoch,
        )
        for service in _STOP_BOUND_SERVICES:
            self.__dict__.pop(service, None)
        self.metric_enrichment = MetricEnrichmentCircuit(check_stop=self.stop.check)

    @cached_property
    def database(self) -> DatabaseRepository:
        """Return one repository configured for this process."""

        return DatabaseRepository(
            DatabaseSettings.from_config(self.config),
            check_stop=self.stop.check,
            sleep=self.stop.sleep,
        )

    @cached_property
    def aggregate_settings(self) -> AggregateSettings:
        """Return aggregate settings captured for this process."""

        return AggregateSettings.from_env()

    @cached_property
    def publication_limits(self) -> PublicationLimits:
        """Return publication limits captured for this process."""

        return PublicationLimits.from_env()

    @cached_property
    def snapshots(self) -> SnapshotStore:
        """Return local snapshot storage configured for this process."""

        return SnapshotStore.from_config(
            self.config,
            check_stop=self.stop.check,
        )

    @cached_property
    def github_settings(self) -> GitHubSettings:
        """Return GitHub settings captured for this process."""

        return GitHubSettings.from_env()

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

    @contextmanager
    def github_client(
        self,
        *,
        report: Callable[[str], None] | None = None,
    ) -> Generator[GitHubClient, None, None]:
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
