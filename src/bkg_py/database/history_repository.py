"""Repository operations for bounded package and version history migration."""

from __future__ import annotations

import sqlite3
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

from . import history_migration, package_history, version_history
from .settings import DatabaseSettings


class HistoryRepositoryMixin(ABC):
    """Add lazy history migration to the shared SQLite repository."""

    settings: DatabaseSettings

    @abstractmethod
    def ensure_schema(self) -> None:
        """Create or migrate the lazy normalized schema."""

        raise NotImplementedError

    @abstractmethod
    def _run_write(self, operation: Callable[[sqlite3.Connection], Any]) -> Any:
        raise NotImplementedError

    def migrate_version_history(
        self,
        row_limit: int,
    ) -> version_history.VersionHistoryMigration:
        """Move one bounded version-observation batch into normalized history."""

        self.ensure_schema()
        return history_migration.migrate(
            self._run_write,
            self.settings.versions_table,
            row_limit,
            version_history.migrate_batch,
        )

    def migrate_package_history(
        self,
        row_limit: int,
    ) -> package_history.PackageHistoryMigration:
        """Move one bounded package-observation batch into normalized history."""

        self.ensure_schema()
        return history_migration.migrate(
            self._run_write,
            self.settings.packages_table,
            row_limit,
            package_history.migrate_batch,
        )
