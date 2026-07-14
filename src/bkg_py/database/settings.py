"""SQLite database settings captured from the shell runtime."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from ..config import RuntimeConfig
from .support import (
    DatabaseError,
    nonnegative_env_float,
    positive_env_int,
)


@dataclass(frozen=True)
class _RetrySettings:
    busy_timeout_ms: int
    max_attempts: int
    retry_delay_seconds: float
    owner_retry_initial_seconds: int
    owner_retry_max_seconds: int


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
    def from_env(cls) -> DatabaseSettings:
        """Read database settings from the Bash-compatible environment."""

        database_path = os.environ.get("BKG_INDEX_DB")
        if not database_path:
            raise DatabaseError("BKG_INDEX_DB is required")
        retry = _env_retry_settings()
        return cls(
            path=Path(database_path),
            owners_table=os.environ.get("BKG_INDEX_TBL_OWN", "owners"),
            packages_table=os.environ.get("BKG_INDEX_TBL_PKG", "packages"),
            versions_table=os.environ.get("BKG_INDEX_TBL_VER", "versions"),
            busy_timeout_ms=retry.busy_timeout_ms,
            max_attempts=retry.max_attempts,
            retry_delay_seconds=retry.retry_delay_seconds,
            owner_retry_initial_seconds=retry.owner_retry_initial_seconds,
            owner_retry_max_seconds=retry.owner_retry_max_seconds,
        )

    @classmethod
    def from_config(cls, config: RuntimeConfig) -> DatabaseSettings:
        """Read database path and table names from captured runtime config."""

        if config.index_db is None:
            raise DatabaseError("BKG_INDEX_DB is required")
        retry = _env_retry_settings()
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


def _env_retry_settings() -> _RetrySettings:
    return _RetrySettings(
        busy_timeout_ms=positive_env_int(
            "BKG_SQLITE_BUSY_TIMEOUT_MS",
            300_000,
        ),
        max_attempts=positive_env_int("BKG_SQLITE_MAX_ATTEMPTS", 3),
        retry_delay_seconds=nonnegative_env_float(
            "BKG_SQLITE_RETRY_DELAY_SECS",
            1.0,
        ),
        owner_retry_initial_seconds=positive_env_int(
            "BKG_OWNER_RETRY_INITIAL_SECONDS",
            3_600,
        ),
        owner_retry_max_seconds=positive_env_int(
            "BKG_OWNER_RETRY_MAX_SECONDS",
            86_400,
        ),
    )
