"""Repository operations for bounded package and version history migration."""

from ..kernel import DatabaseComponent
from . import migration as history_migration
from . import package_history, version_history


class HistoryRepository(DatabaseComponent):
    """Provide bounded lazy history migration."""

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
