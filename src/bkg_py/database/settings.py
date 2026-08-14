"""SQLite database settings captured from the shell runtime."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from ..config import ConfigError, RuntimeConfig, read_float, read_int
from ..runtime_names import EnvironmentVariable as Env
from .support import DatabaseError


@dataclass(frozen=True)
class DatabaseTuning:
    """SQLite retry and owner-backoff settings independent of one path."""

    busy_timeout_ms: int = 300_000
    max_attempts: int = 3
    retry_delay_seconds: float = 1.0
    owner_retry_initial_seconds: int = 3_600
    owner_retry_max_seconds: int = 86_400

    @classmethod
    def from_mapping(cls, values: Mapping[str, str]) -> DatabaseTuning:
        """Load SQLite tuning from one captured configuration mapping."""

        settings = cls(
            busy_timeout_ms=read_int(
                values,
                Env.BKG_SQLITE_BUSY_TIMEOUT_MS,
                300_000,
                minimum=1,
            ),
            max_attempts=read_int(
                values,
                Env.BKG_SQLITE_MAX_ATTEMPTS,
                3,
                minimum=1,
            ),
            retry_delay_seconds=read_float(
                values,
                Env.BKG_SQLITE_RETRY_DELAY_SECS,
                1.0,
                minimum=0,
            ),
            owner_retry_initial_seconds=read_int(
                values,
                Env.BKG_OWNER_RETRY_INITIAL_SECONDS,
                3_600,
                minimum=1,
            ),
            owner_retry_max_seconds=read_int(
                values,
                Env.BKG_OWNER_RETRY_MAX_SECONDS,
                86_400,
                minimum=1,
            ),
        )
        if settings.owner_retry_max_seconds < settings.owner_retry_initial_seconds:
            raise ConfigError(
                f"{Env.BKG_OWNER_RETRY_MAX_SECONDS} must be at least "
                f"{Env.BKG_OWNER_RETRY_INITIAL_SECONDS}"
            )
        return settings


@dataclass(frozen=True)
class DatabaseSettings:  # pylint: disable=too-many-instance-attributes
    """Database path, table names, and retry behavior."""

    path: Path
    owners_table: str = "owners"
    packages_table: str = "packages"
    versions_table: str = "versions"
    busy_timeout_ms: int = 300_000
    max_attempts: int = 3
    retry_delay_seconds: float = 1.0
    owner_retry_initial_seconds: int = 3_600
    owner_retry_max_seconds: int = 86_400

    @classmethod
    def from_config(
        cls,
        config: RuntimeConfig,
        tuning: DatabaseTuning | None = None,
    ) -> DatabaseSettings:
        """Read database path and table names from captured runtime config."""

        if config.index_db is None:
            raise DatabaseError("BKG_INDEX_DB is required")
        retry = tuning or DatabaseTuning()
        return cls(
            path=Path(config.index_db),
            owners_table=config.owners_table,
            packages_table=config.packages_table,
            versions_table=config.versions_table,
            busy_timeout_ms=retry.busy_timeout_ms,
            max_attempts=retry.max_attempts,
            retry_delay_seconds=retry.retry_delay_seconds,
            owner_retry_initial_seconds=retry.owner_retry_initial_seconds,
            owner_retry_max_seconds=retry.owner_retry_max_seconds,
        )
