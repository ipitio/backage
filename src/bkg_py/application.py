"""Construct and share bkg runtime services for one Python operation."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

from .config import RuntimeConfig
from .database import DatabaseRepository, DatabaseSettings
from .github import (
    GitHubClient,
    GitHubRateAccounting,
    GitHubRuntime,
    GitHubSettings,
)
from .publication import PublicationLimits
from .rendering import AggregateSettings
from .runtime import StopController
from .state import StateStore


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
            DatabaseSettings.from_env(),
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
    def github_settings(self) -> GitHubSettings:
        """Return GitHub settings captured for this process."""

        return GitHubSettings.from_env()

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
