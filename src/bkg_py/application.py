"""Construct and share bkg runtime services for one Python operation."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

from .concurrency import BoundedWorkerRunner, ConcurrencySettings
from .config import RuntimeConfig
from .database import DatabaseRepository
from .database_settings import DatabaseSettings
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


@dataclass
class ApplicationContext:
    """Shared configuration and services for one bkg process."""

    config: RuntimeConfig
    state: StateStore
    stop: StopController

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
    def github_client(self) -> Generator[GitHubClient, None, None]:
        """Yield a pooled client connected to this process's state and stop control."""

        self.ensure_state_file()
        with GitHubClient(
            self.github_settings,
            accounting=GitHubRateAccounting(self.state),
            runtime=GitHubRuntime(
                check_stop=self.stop.check,
                sleep=self.stop.sleep,
            ),
        ) as client:
            yield client
