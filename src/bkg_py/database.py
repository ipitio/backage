"""SQLite repository for bkg's normalized and legacy package metadata."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from typing import Any

from . import database_owner_scans, database_packages, database_version_stages
from .database_models import (
    OwnerRecord,
    PackageRecord,
    PackageRef,
    PackageSnapshot,
    PackageWorkItem,
    PackageWorkPlan,
    RankedPackage,
    VersionLimitEstimate,
    VersionRecord,
    VersionSource,
    VersionStage,
)
from .database_owner_identities import OwnerIdentityRepositoryMixin
from .database_owner_repository import OwnerScanRepositoryMixin
from .database_settings import DatabaseSettings
from .database_support import DatabaseError
from .database_values import (
    package_sort_key as _package_sort_key,
)
from .database_values import (
    package_values as _package_values,
)
from .database_values import (
    ranked_package as _ranked_package,
)
from .database_values import (
    version_record as _version_record,
)
from .database_values import (
    version_records as _version_records,
)
from .render_sql import (
    OWNER_VERSION_LIMIT_SQL,
    OWNER_VERSION_ROWS_SQL,
    PACKAGE_SNAPSHOT_SQL,
    RANKED_PACKAGES_SQL,
)
from .schema_sql import OWNER_SCAN_SCHEMA_MIGRATIONS, SCHEMA_SQL

_RETRYABLE_MESSAGES = (
    "database is locked",
    "database is busy",
    "database schema is locked",
    "locking protocol",
    "cannot commit transaction",
    "disk i/o error",
)


class _SqlIdentifier(str):
    """A SQLite identifier quoted before it can enter a statement."""

    def __new__(cls, value: str) -> _SqlIdentifier:
        if "\x00" in value:
            raise DatabaseError("SQLite identifiers cannot contain NUL")
        quoted = f'"{value.replace(chr(34), chr(34) * 2)}"'
        return str.__new__(cls, quoted)


class DatabaseRepository(  # pylint: disable=too-many-public-methods
    OwnerIdentityRepositoryMixin,
    OwnerScanRepositoryMixin,
):
    """Own bkg's SQLite schema, transactions, fallback reads, and cleanup."""

    def __init__(
        self,
        settings: DatabaseSettings,
        *,
        check_stop: Callable[[], None] = lambda: None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.settings = settings
        self._check_stop = check_stop
        self._sleep = sleep

    def ensure_schema(self) -> None:
        """Lazily create normalized tables and their query indexes."""

        owners = _SqlIdentifier(self.settings.owners_table)
        packages = _SqlIdentifier(self.settings.packages_table)
        versions = _SqlIdentifier(self.settings.versions_table)
        statements = tuple(
            _sql(
                statement,
                owners=owners,
                packages=packages,
                versions=versions,
            )
            for statement in SCHEMA_SQL
        )

        def create(connection: sqlite3.Connection) -> None:
            with _transaction(connection):
                for statement in statements:
                    connection.execute(statement)
                owner_scan_columns = {
                    str(row[1])
                    for row in connection.execute(
                        'pragma table_info("bkg_owner_scans")'
                    )
                }
                for column, statement in OWNER_SCAN_SCHEMA_MIGRATIONS:
                    if column not in owner_scan_columns:
                        connection.execute(statement)

        self._run_write(create)

    def write_owner(self, record: OwnerRecord) -> None:
        """Insert or replace one owner scan record."""

        self.ensure_schema()
        self._run_write(
            lambda connection: database_owner_scans.write_owner(
                connection,
                self.settings.owners_table,
                record,
            )
        )

    def write_package(self, record: PackageRecord) -> None:
        """Insert or replace one normalized package record."""

        self._write_package(record, publication_pending=False)

    def write_package_pending_publication(self, record: PackageRecord) -> None:
        """Commit package metadata and its publication marker together."""

        self._write_package(record, publication_pending=True)

    def _write_package(
        self,
        record: PackageRecord,
        *,
        publication_pending: bool,
    ) -> None:
        """Insert one package row, optionally marking generated files stale."""

        self.ensure_schema()
        self._run_write(
            lambda connection: database_packages.write(
                connection,
                self.settings.packages_table,
                record,
                mark_pending=publication_pending,
            )
        )

    def flush_version_stage(
        self,
        stage: VersionStage,
        *,
        publication_pending_at: str | None = None,
    ) -> int:
        """Commit every staged version row in one retryable transaction."""

        self.ensure_schema()
        return self._flush_version_stage(
            stage,
            finalizing=False,
            publication_pending_at=publication_pending_at,
        )

    def finalize_version_stage(
        self,
        stage: VersionStage,
        *,
        publication_pending_at: str | None = None,
    ) -> int:
        """Commit completed version rows after a stop has already been requested."""

        return self._flush_version_stage(
            stage,
            finalizing=True,
            publication_pending_at=publication_pending_at,
        )

    def _flush_version_stage(
        self,
        stage: VersionStage,
        *,
        finalizing: bool,
        publication_pending_at: str | None,
    ) -> int:
        if not stage.rows:
            return 0

        def flush(connection: sqlite3.Connection) -> None:
            database_version_stages.flush(
                connection,
                self.settings.versions_table,
                stage,
                publication_pending_at,
            )

        if finalizing:
            self._run_final_write(flush)
        else:
            self._run_write(flush)
        return len(stage.rows)

    def package_updated_since(self, package: PackageRef, since: str) -> bool:
        """Return whether one package has a normalized row in this batch."""

        self.ensure_schema()
        return self._run_read(
            lambda connection: database_packages.updated_since(
                connection,
                self.settings.packages_table,
                package,
                since,
            )
        )

    def package_work_plan(self, since: str) -> PackageWorkPlan:
        """Read the current package batch plan from one database snapshot."""

        self.ensure_schema()
        packages = _SqlIdentifier(self.settings.packages_table)
        owners = _SqlIdentifier(self.settings.owners_table)

        def load(connection: sqlite3.Connection) -> PackageWorkPlan:
            with _read_snapshot(connection):
                package_rows = connection.execute(
                    _sql(
                        """
                        select owner_id, owner, repo, package,
                               max(date) as max_date
                        from {packages}
                        group by owner_id, owner, repo, package
                        order by max_date asc
                        """,
                        packages=packages,
                    )
                ).fetchall()
                completed_rows = connection.execute(
                    _sql(
                        """
                        select current.owner_id, current.owner, current.repo,
                               current.package, max(current.date) as max_date
                        from {packages} current
                        where not exists (
                            select 1
                            from bkg_package_publications pending
                            where pending.owner_id = current.owner_id
                              and pending.owner_type = current.owner_type
                              and pending.package_type = current.package_type
                              and pending.owner = current.owner
                              and pending.repo = current.repo
                              and pending.package = current.package
                        )
                        group by current.owner_id, current.owner,
                                 current.repo, current.package
                        having max(current.date) >= ?
                        order by max_date asc
                        """,
                        packages=packages,
                    ),
                    (since,),
                ).fetchall()
                owner_rows = connection.execute(
                    _sql(
                        """
                        select owner
                        from (
                            select owner, min(date) as first_date
                            from {packages}
                            group by owner
                            union all
                            select owner, min(date) as first_date
                            from {owners}
                            where date >= ?
                            group by owner
                        )
                        group by owner
                        order by min(first_date), owner
                        """,
                        packages=packages,
                        owners=owners,
                    ),
                    (since,),
                ).fetchall()
                empty_owner_rows = connection.execute(
                    _sql(
                        """
                        select owner from {owners}
                        where date >= ?
                        order by owner asc
                        """,
                        owners=owners,
                    ),
                    (since,),
                ).fetchall()

            all_packages = tuple(_package_work_item(row) for row in package_rows)
            completed = tuple(_package_work_item(row) for row in completed_rows)
            completed_set = set(completed)
            return PackageWorkPlan(
                all_packages,
                completed,
                tuple(item for item in all_packages if item not in completed_set),
                tuple(str(row[0]) for row in owner_rows),
                tuple(str(row[0]) for row in empty_owner_rows),
            )

        return self._run_read(load)

    def maximum_package_downloads(self, package: PackageRef) -> int:
        """Return the largest previously stored total download count."""

        self.ensure_schema()
        return self._run_read(
            lambda connection: database_packages.maximum_downloads(
                connection,
                self.settings.packages_table,
                package,
            )
        )

    def mark_package_publication_pending(
        self,
        package: PackageRef,
        updated_at: str,
    ) -> None:
        """Persist that one package's generated files need replacement."""

        self.ensure_schema()
        self._run_write(
            lambda connection: database_packages.mark_publication_pending_transaction(
                connection,
                package,
                updated_at,
            )
        )

    def package_publication_pending(self, package: PackageRef) -> bool:
        """Return whether one package still needs generated-file publication."""

        self.ensure_schema()
        return self._run_read(
            lambda connection: database_packages.publication_pending(
                connection,
                package,
            )
        )

    def clear_package_publication(self, package: PackageRef) -> None:
        """Clear one package marker after JSON and XML are both published."""

        self.ensure_schema()
        self._run_write(
            lambda connection: database_packages.clear_publication_transaction(
                connection,
                package,
            )
        )

    def retire_package(self, package: PackageRef) -> None:
        """Delete one opted-out package, its versions, marker, and legacy table."""

        self.ensure_schema()

        self._run_write(
            lambda connection: database_packages.retire(
                connection,
                self.settings.packages_table,
                self.settings.versions_table,
                self._legacy_version_table(package),
                package,
            )
        )

    def version_rows(
        self,
        package: PackageRef,
        *,
        since: str = "0000-00-00",
        legacy_table: str | None = None,
    ) -> VersionSource:
        """Read normalized rows first and use a legacy table only as fallback."""

        self.ensure_schema()

        def read(connection: sqlite3.Connection) -> VersionSource:
            return self._version_source(
                connection,
                package,
                since,
                legacy_table=legacy_table,
            )

        return self._run_read(read)

    def package_snapshot(
        self,
        package: PackageRef,
        *,
        since: str,
        legacy_table: str | None = None,
    ) -> PackageSnapshot | None:
        """Return the latest ranked package row and its version source."""

        self.ensure_schema()

        def read(connection: sqlite3.Connection) -> PackageSnapshot | None:
            packages = _SqlIdentifier(self.settings.packages_table)
            row = connection.execute(
                _sql(PACKAGE_SNAPSHOT_SQL, packages=packages),
                (
                    *_package_values(package),
                    package.owner_id,
                    package.owner_id,
                    package.repo,
                    package.owner_id,
                    package.owner_id,
                    package.repo,
                ),
            ).fetchone()
            if row is None:
                return None
            ranked = _ranked_package(row)
            return PackageSnapshot(
                ranked,
                self._version_source(
                    connection,
                    package,
                    since,
                    legacy_table=legacy_table,
                ),
            )

        return self._run_read(read)

    def visit_owner_snapshots(
        self,
        owner_id: str,
        *,
        repo: str | None,
        visit: Callable[[PackageSnapshot], None],
    ) -> int:
        """Visit latest owner packages without retaining all rendered versions."""

        self.ensure_schema()

        def read(connection: sqlite3.Connection) -> int:
            count = 0
            groups = self._normalized_owner_version_groups(
                connection,
                owner_id,
                repo=repo,
            )
            current_group = next(groups, None)
            for ranked in self._ranked_package_rows(
                connection,
                owner_id,
                repo=repo,
            ):
                package = ranked.record.package_ref
                package_values = _package_values(package)
                if current_group is not None and current_group[0] == package_values:
                    source = VersionSource("normalized", current_group[1])
                    current_group = next(groups, None)
                else:
                    if current_group is not None and _package_sort_key(
                        current_group[0]
                    ) < _package_sort_key(package_values):
                        raise DatabaseError(
                            "owner version rows are out of package order"
                        )
                    source = self._legacy_version_source(
                        connection,
                        self._legacy_version_table(package),
                        ranked.record.date,
                    )
                visit(
                    PackageSnapshot(
                        ranked,
                        source,
                    )
                )
                count += 1
            if current_group is not None:
                raise DatabaseError("owner version rows do not match package rows")
            return count

        return self._run_read(read)

    def repository_names(self, owner_id: str) -> tuple[str, ...]:
        """Return repository names for one owner in deterministic order."""

        self.ensure_schema()
        packages = _SqlIdentifier(self.settings.packages_table)

        def read(connection: sqlite3.Connection) -> tuple[str, ...]:
            rows = connection.execute(
                _sql(
                    """
                    select distinct repo
                    from {packages}
                    where owner_id = ?
                    order by repo
                    """,
                    packages=packages,
                ),
                (owner_id,),
            ).fetchall()
            return tuple(str(row[0]) for row in rows)

        return self._run_read(read)

    def estimate_owner_version_limit(
        self,
        owner_id: str,
        estimate: VersionLimitEstimate,
    ) -> int:
        """Estimate one deterministic per-package version limit for an aggregate."""

        self.ensure_schema()
        packages = _SqlIdentifier(self.settings.packages_table)
        versions = _SqlIdentifier(self.settings.versions_table)
        effective_target = max(
            1,
            estimate.target_bytes * estimate.headroom_percent // 100,
        )
        prefix = f"{self.settings.versions_table}_"
        parameters: list[str | int | None] = [
            owner_id,
            estimate.repo,
            estimate.repo,
        ]
        parameters.extend(
            (
                prefix,
                effective_target,
                estimate.fallback_limit,
                effective_target,
            )
        )

        statement = _sql(
            OWNER_VERSION_LIMIT_SQL,
            packages=packages,
            versions=versions,
        )

        def read(connection: sqlite3.Connection) -> int:
            row = connection.execute(statement, parameters).fetchone()
            return estimate.fallback_limit if row is None else int(row[0])

        return self._run_read(read)

    def _legacy_version_table(self, package: PackageRef) -> str:
        """Return the legacy per-package version table name."""

        return (
            f"{self.settings.versions_table}_{package.owner_type}_"
            f"{package.package_type}_{package.owner}_{package.repo}_{package.package}"
        )

    def cleanup_legacy_package(
        self,
        package: PackageRef,
        legacy_table: str,
        *,
        since: str,
    ) -> bool:
        """Prune one legacy table and drop it only after verified replacement."""

        self.ensure_schema()

        def cleanup(connection: sqlite3.Connection) -> bool:
            with _transaction(connection):
                return self._cleanup_legacy_table(
                    connection,
                    legacy_table,
                    package,
                    since,
                )

        return self._run_write(cleanup)

    def cleanup_replaced_legacy_tables(
        self,
        *,
        since: str,
        prune_normalized: bool = False,
        vacuum: bool = False,
    ) -> int:
        """Prune all legacy version tables, including orphaned rotation debris."""

        self.ensure_schema()
        if prune_normalized:
            self._prune_normalized_rows(since)

        def cleanup(connection: sqlite3.Connection) -> int:
            prefix = f"{self.settings.versions_table}_"
            table_rows = connection.execute(
                """
                select name
                from sqlite_master
                where type = 'table'
                  and substr(name, 1, length(?)) = ?
                order by name
                """,
                (prefix, prefix),
            ).fetchall()
            dropped = 0
            with _transaction(connection):
                for row in table_rows:
                    table_name = str(row[0])
                    package = self._package_for_legacy_table(
                        connection,
                        table_name,
                        since,
                    )
                    if package is None:
                        connection.execute(
                            _sql(
                                "drop table if exists {table}",
                                table=_SqlIdentifier(table_name),
                            )
                        )
                        dropped += 1
                    elif self._cleanup_legacy_table(
                        connection,
                        table_name,
                        package,
                        since,
                    ):
                        dropped += 1
            return dropped

        dropped = self._run_write(cleanup)
        if vacuum:
            self._run_write(lambda connection: connection.execute("vacuum"))
        return dropped

    def _prune_normalized_rows(self, since: str) -> None:
        def prune_normalized(connection: sqlite3.Connection) -> None:
            packages = _SqlIdentifier(self.settings.packages_table)
            versions = _SqlIdentifier(self.settings.versions_table)
            with _transaction(connection):
                connection.execute(
                    _sql("delete from {packages} where date < ?", packages=packages),
                    (since,),
                )
                connection.execute(
                    _sql("delete from {versions} where date < ?", versions=versions),
                    (since,),
                )

        self._run_write(prune_normalized)

    def retire_owner(self, owner: str) -> int:
        """Remove one unavailable owner's normalized and legacy database data."""

        if not owner:
            raise DatabaseError("owner is required")
        self.ensure_schema()
        tables = database_owner_scans.OwnerScanTables(
            self.settings.owners_table,
            self.settings.packages_table,
            self.settings.versions_table,
        )
        return self._run_write(
            lambda connection: database_owner_scans.retire_owner(
                connection,
                owner,
                tables,
            )
        )

    def _normalized_version_rows(
        self,
        connection: sqlite3.Connection,
        package: PackageRef,
        since: str,
    ) -> tuple[VersionRecord, ...]:
        versions = _SqlIdentifier(self.settings.versions_table)
        rows = connection.execute(
            _sql(
                """
            select id, name, size, downloads, downloads_month,
                   downloads_week, downloads_day, date, tags
            from {versions}
            where owner_id = ?
              and owner_type = ?
              and package_type = ?
              and owner = ?
              and repo = ?
              and package = ?
              and date >= ?
            """,
                versions=versions,
            ),
            (*_package_values(package), since),
        ).fetchall()
        return _version_records(rows)

    def _version_source(
        self,
        connection: sqlite3.Connection,
        package: PackageRef,
        since: str,
        *,
        legacy_table: str | None,
    ) -> VersionSource:
        normalized = self._normalized_version_rows(connection, package, since)
        if normalized:
            return VersionSource("normalized", normalized)
        if legacy_table:
            return self._legacy_version_source(connection, legacy_table, since)
        return VersionSource("normalized", ())

    def _legacy_version_source(
        self,
        connection: sqlite3.Connection,
        legacy_table: str,
        since: str,
    ) -> VersionSource:
        if not _table_exists(connection, legacy_table):
            return VersionSource("normalized", ())
        legacy = _SqlIdentifier(legacy_table)
        rows = connection.execute(
            _sql(
                """
                select id, name, size, downloads, downloads_month,
                       downloads_week, downloads_day, date, tags
                from {legacy}
                where date >= ?
                """,
                legacy=legacy,
            ),
            (since,),
        ).fetchall()
        return VersionSource("legacy", _version_records(rows))

    def _normalized_owner_version_groups(
        self,
        connection: sqlite3.Connection,
        owner_id: str,
        *,
        repo: str | None,
    ) -> Generator[tuple[tuple[str, ...], tuple[VersionRecord, ...]], None, None]:
        packages = _SqlIdentifier(self.settings.packages_table)
        versions = _SqlIdentifier(self.settings.versions_table)
        cursor = connection.execute(
            _sql(
                OWNER_VERSION_ROWS_SQL,
                packages=packages,
                versions=versions,
            ),
            (owner_id, repo, repo),
        )
        current_key: tuple[str, ...] | None = None
        current_rows: list[VersionRecord] = []
        for row in cursor:
            key = tuple(str(value) for value in row[:6])
            if current_key is not None and key != current_key:
                yield current_key, tuple(current_rows)
                current_rows = []
            current_key = key
            current_rows.append(_version_record(row[6:]))
        if current_key is not None:
            yield current_key, tuple(current_rows)

    def _ranked_package_rows(
        self,
        connection: sqlite3.Connection,
        owner_id: str,
        *,
        repo: str | None,
    ) -> tuple[RankedPackage, ...]:
        packages = _SqlIdentifier(self.settings.packages_table)
        parameters = (owner_id, repo, repo)
        rows = connection.execute(
            _sql(RANKED_PACKAGES_SQL, packages=packages),
            parameters,
        ).fetchall()
        return tuple(_ranked_package(row) for row in rows)

    def _package_for_legacy_table(
        self,
        connection: sqlite3.Connection,
        table_name: str,
        since: str,
    ) -> PackageRef | None:
        packages = _SqlIdentifier(self.settings.packages_table)
        prefix = f"{self.settings.versions_table}_"
        row = connection.execute(
            _sql(
                """
            select owner_id, owner_type, package_type, owner, repo, package
            from {packages}
            where date >= ?
              and (? || owner_type || '_' || package_type || '_' || owner
                   || '_' || repo || '_' || package) = ?
            order by date desc
            limit 1
            """,
                packages=packages,
            ),
            (since, prefix, table_name),
        ).fetchone()
        if row is None:
            return None
        return PackageRef(*(str(value) for value in row))

    def _cleanup_legacy_table(
        self,
        connection: sqlite3.Connection,
        table_name: str,
        package: PackageRef,
        since: str,
    ) -> bool:
        if not _table_exists(connection, table_name):
            return False
        legacy = _SqlIdentifier(table_name)
        connection.execute(
            _sql("delete from {legacy} where date < ?", legacy=legacy),
            (since,),
        )
        current_count = int(
            connection.execute(
                _sql(
                    "select count(*) from {legacy} where date >= ?",
                    legacy=legacy,
                ),
                (since,),
            ).fetchone()[0]
        )
        if current_count == 0:
            connection.execute(_sql("drop table if exists {legacy}", legacy=legacy))
            return True

        versions = _SqlIdentifier(self.settings.versions_table)
        missing = int(
            connection.execute(
                _sql(
                    """
                select count(*)
                from {legacy} legacy
                where legacy.date >= ?
                  and not exists (
                    select 1
                    from {versions} normalized
                    where normalized.owner_id = ?
                      and normalized.owner_type = ?
                      and normalized.package_type = ?
                      and normalized.owner = ?
                      and normalized.repo = ?
                      and normalized.package = ?
                      and normalized.id = legacy.id
                      and normalized.date = legacy.date
                  )
                """,
                    legacy=legacy,
                    versions=versions,
                ),
                (since, *_package_values(package)),
            ).fetchone()[0]
        )
        if missing != 0:
            return False
        connection.execute(_sql("drop table if exists {legacy}", legacy=legacy))
        return True

    def _run_read(self, operation: Callable[[sqlite3.Connection], Any]) -> Any:
        return self._run(operation, retry=False)

    def _run_write(self, operation: Callable[[sqlite3.Connection], Any]) -> Any:
        return self._run(operation, retry=True)

    def _run_final_write(self, operation: Callable[[sqlite3.Connection], Any]) -> Any:
        return self._run(operation, retry=False, observe_stop=False)

    def _run(
        self,
        operation: Callable[[sqlite3.Connection], Any],
        *,
        retry: bool,
        observe_stop: bool = True,
    ) -> Any:
        attempt = 1
        while True:
            if observe_stop:
                self._check_stop()
            try:
                with self._connection() as connection:
                    return operation(connection)
            except sqlite3.Error as error:
                if (
                    not retry
                    or not _is_retryable(error)
                    or attempt >= self.settings.max_attempts
                ):
                    raise DatabaseError(str(error)) from error
                if observe_stop:
                    self._check_stop()
                self._sleep(self.settings.retry_delay_seconds)
                attempt += 1

    @contextmanager
    def _connection(self) -> Generator[sqlite3.Connection, None, None]:
        self.settings.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            self.settings.path,
            timeout=self.settings.busy_timeout_ms / 1000,
            isolation_level=None,
        )
        try:
            connection.execute(f"pragma busy_timeout = {self.settings.busy_timeout_ms}")
            connection.execute("pragma synchronous = normal")
            connection.execute("pragma foreign_keys = on")
            connection.execute("pragma journal_mode = wal")
            connection.execute("pragma locking_mode = normal")
            connection.execute("pragma temp_store = memory")
            connection.execute("pragma wal_autocheckpoint = 1000")
            connection.execute("pragma cache_size = -500000")
            yield connection
        finally:
            connection.close()


def _sql(statement: str, /, **identifiers: _SqlIdentifier) -> str:
    return statement.format_map(identifiers)


def _package_work_item(row: tuple[Any, ...]) -> PackageWorkItem:
    return PackageWorkItem(*(str(value) for value in row))


def _table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    return (
        connection.execute(
            """
            select 1
            from sqlite_master
            where type = 'table' and name = ?
            limit 1
            """,
            (table_name,),
        ).fetchone()
        is not None
    )


@contextmanager
def _transaction(connection: sqlite3.Connection) -> Generator[None, None, None]:
    connection.execute("begin immediate")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    connection.commit()


@contextmanager
def _read_snapshot(connection: sqlite3.Connection) -> Generator[None, None, None]:
    connection.execute("begin")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    connection.commit()


def _is_retryable(error: sqlite3.Error) -> bool:
    message = str(error).lower()
    return any(fragment in message for fragment in _RETRYABLE_MESSAGES)
