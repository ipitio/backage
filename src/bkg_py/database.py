"""SQLite repository for bkg's normalized and legacy package metadata."""

from __future__ import annotations

import os
import sqlite3
import time
from collections.abc import Callable, Generator, Iterable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .database_support import (
    DatabaseError,
    load_object,
    nonnegative_env_float,
    optional_text,
    positive_env_int,
    required_int,
    required_string,
    required_text,
)
from .render_sql import (
    OWNER_VERSION_LIMIT_SQL,
    OWNER_VERSION_ROWS_SQL,
    PACKAGE_SNAPSHOT_SQL,
    RANKED_PACKAGES_SQL,
)
from .schema_sql import SCHEMA_SQL

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


@dataclass(frozen=True)
class DatabaseSettings:
    """Database path, table names, and retry behavior."""

    path: Path
    owners_table: str = "owners"
    packages_table: str = "packages"
    versions_table: str = "versions"
    busy_timeout_ms: int = 300_000
    max_attempts: int = 3
    retry_delay_seconds: float = 1.0

    @classmethod
    def from_env(cls) -> DatabaseSettings:
        """Read database settings from the Bash-compatible environment."""

        database_path = os.environ.get("BKG_INDEX_DB")
        if not database_path:
            raise DatabaseError("BKG_INDEX_DB is required")
        return cls(
            path=Path(database_path),
            owners_table=os.environ.get("BKG_INDEX_TBL_OWN", "owners"),
            packages_table=os.environ.get("BKG_INDEX_TBL_PKG", "packages"),
            versions_table=os.environ.get("BKG_INDEX_TBL_VER", "versions"),
            busy_timeout_ms=positive_env_int(
                "BKG_SQLITE_BUSY_TIMEOUT_MS",
                300_000,
            ),
            max_attempts=positive_env_int("BKG_SQLITE_MAX_ATTEMPTS", 3),
            retry_delay_seconds=nonnegative_env_float(
                "BKG_SQLITE_RETRY_DELAY_SECS",
                1.0,
            ),
        )


@dataclass(frozen=True)
class PackageRef:
    """Columns that identify one package across normalized tables."""

    owner_id: str
    owner_type: str
    package_type: str
    owner: str
    repo: str
    package: str


@dataclass(frozen=True)
class OwnerRecord:
    """One normalized owner scan record."""

    owner_id: str
    owner: str
    date: str


@dataclass(frozen=True)
class PackageRecord:
    """One normalized package metadata record."""

    package_ref: PackageRef
    downloads: int
    downloads_month: int
    downloads_week: int
    downloads_day: int
    size: int
    date: str


@dataclass(frozen=True)
class RankedPackage:
    """A latest package row with owner-wide and repository-local ranks."""

    record: PackageRecord
    owner_rank: int
    repo_rank: int


@dataclass(frozen=True)
class VersionMetrics:
    """Raw size and download counters for one version."""

    size: int
    downloads: int
    downloads_month: int
    downloads_week: int
    downloads_day: int


@dataclass(frozen=True)
class VersionRecord:
    """One normalized package-version record."""

    version_id: str
    name: str
    metrics: VersionMetrics
    date: str
    tags: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> VersionRecord:
        """Validate and decode one staged JSON record."""

        return cls(
            version_id=required_text(value, "id"),
            name=required_text(value, "name"),
            metrics=VersionMetrics(
                size=required_int(value, "size"),
                downloads=required_int(value, "downloads"),
                downloads_month=required_int(value, "downloads_month"),
                downloads_week=required_int(value, "downloads_week"),
                downloads_day=required_int(value, "downloads_day"),
            ),
            date=required_text(value, "date"),
            tags=optional_text(value, "tags"),
        )


@dataclass(frozen=True)
class VersionSource:
    """Version rows and the normalized or legacy source that supplied them."""

    source: str
    rows: tuple[VersionRecord, ...]


@dataclass(frozen=True)
class PackageSnapshot:
    """One ranked package and the version source used to render it."""

    package: RankedPackage
    versions: VersionSource


@dataclass(frozen=True)
class VersionLimitEstimate:
    """Inputs for estimating a bounded owner aggregate version slice."""

    repo: str | None
    target_bytes: int
    headroom_percent: int
    fallback_limit: int


@dataclass(frozen=True)
class VersionStage:
    """A complete package identity and its staged version rows."""

    package_ref: PackageRef
    legacy_table: str
    write_legacy: bool
    rows: tuple[VersionRecord, ...]

    @classmethod
    def load(cls, directory: Path) -> VersionStage:
        """Load a manifest and all row files without modifying the stage."""

        manifest = load_object(directory / "manifest.json")
        package_ref = PackageRef(
            owner_id=required_string(manifest, "owner_id"),
            owner_type=required_text(manifest, "owner_type"),
            package_type=required_text(manifest, "package_type"),
            owner=required_text(manifest, "owner"),
            repo=required_text(manifest, "repo"),
            package=required_text(manifest, "package"),
        )
        legacy_table = required_text(manifest, "legacy_table")
        write_legacy = manifest.get("write_legacy")
        if not isinstance(write_legacy, bool):
            raise DatabaseError("stage field 'write_legacy' must be a boolean")

        row_paths = sorted(directory.glob("row.*.json"))
        rows = tuple(
            VersionRecord.from_mapping(load_object(path)) for path in row_paths
        )
        return cls(
            package_ref=package_ref,
            legacy_table=legacy_table,
            write_legacy=write_legacy,
            rows=rows,
        )


class DatabaseRepository:
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

        self._run_write(create)

    def write_owner(self, record: OwnerRecord) -> None:
        """Insert or replace one owner scan record."""

        self.ensure_schema()
        table = _SqlIdentifier(self.settings.owners_table)
        self._run_write(
            lambda connection: _execute_transaction(
                connection,
                _sql(
                    """
                insert or replace into {table} (owner_id, owner, date)
                values (?, ?, ?)
                """,
                    table=table,
                ),
                (record.owner_id, record.owner, record.date),
            )
        )

    def write_package(self, record: PackageRecord) -> None:
        """Insert or replace one normalized package record."""

        self.ensure_schema()
        table = _SqlIdentifier(self.settings.packages_table)
        package = record.package_ref
        parameters = (
            package.owner_id,
            package.owner_type,
            package.package_type,
            package.owner,
            package.repo,
            package.package,
            record.downloads,
            record.downloads_month,
            record.downloads_week,
            record.downloads_day,
            record.size,
            record.date,
        )
        self._run_write(
            lambda connection: _execute_transaction(
                connection,
                _sql(
                    """
                insert or replace into {table} (
                    owner_id, owner_type, package_type, owner, repo, package,
                    downloads, downloads_month, downloads_week, downloads_day,
                    size, date
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                    table=table,
                ),
                parameters,
            )
        )

    def flush_version_stage(self, stage: VersionStage) -> int:
        """Commit every staged version row in one retryable transaction."""

        self.ensure_schema()
        if not stage.rows:
            return 0
        versions = _SqlIdentifier(self.settings.versions_table)
        normalized_sql = _sql(
            """
            insert or replace into {versions} (
                owner_id, owner_type, package_type, owner, repo, package,
                id, name, size, downloads, downloads_month, downloads_week,
                downloads_day, date, tags
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            versions=versions,
        )
        normalized_rows = tuple(
            _normalized_version_values(stage.package_ref, row) for row in stage.rows
        )

        def flush(connection: sqlite3.Connection) -> None:
            with _transaction(connection):
                connection.executemany(normalized_sql, normalized_rows)
                if stage.write_legacy and _table_exists(
                    connection,
                    stage.legacy_table,
                ):
                    legacy = _SqlIdentifier(stage.legacy_table)
                    connection.executemany(
                        _sql(
                            """
                        insert or replace into {legacy} (
                            id, name, size, downloads, downloads_month,
                            downloads_week, downloads_day, date, tags
                        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                            legacy=legacy,
                        ),
                        tuple(_legacy_version_values(row) for row in stage.rows),
                    )

        self._run_write(flush)
        return len(stage.rows)

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
                        self.legacy_version_table(package),
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

    def legacy_version_table(self, package: PackageRef) -> str:
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

    def cleanup_replaced_legacy_tables(self, *, since: str) -> int:
        """Prune all legacy version tables, including orphaned rotation debris."""

        self.ensure_schema()

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

        return self._run_write(cleanup)

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

    def _run(
        self,
        operation: Callable[[sqlite3.Connection], Any],
        *,
        retry: bool,
    ) -> Any:
        attempt = 1
        while True:
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


def _package_values(package: PackageRef) -> tuple[str, ...]:
    return (
        package.owner_id,
        package.owner_type,
        package.package_type,
        package.owner,
        package.repo,
        package.package,
    )


def _package_sort_key(values: Sequence[str]) -> tuple[str, ...]:
    owner_id, owner_type, package_type, owner, repo, package = values
    return owner, repo, package_type, package, owner_type, owner_id


def _ranked_package(row: Sequence[Any]) -> RankedPackage:
    package = PackageRef(
        owner_id=str(row[0]),
        owner_type=str(row[1]),
        package_type=str(row[2]),
        owner=str(row[3]),
        repo=str(row[4]),
        package=str(row[5]),
    )
    return RankedPackage(
        record=PackageRecord(
            package_ref=package,
            downloads=int(row[6]),
            downloads_month=int(row[7]),
            downloads_week=int(row[8]),
            downloads_day=int(row[9]),
            size=int(row[10]),
            date=str(row[11]),
        ),
        owner_rank=int(row[12]),
        repo_rank=int(row[13]),
    )


def _normalized_version_values(
    package: PackageRef,
    version: VersionRecord,
) -> tuple[str | int, ...]:
    return (*_package_values(package), *_legacy_version_values(version))


def _legacy_version_values(version: VersionRecord) -> tuple[str | int, ...]:
    metrics = version.metrics
    return (
        version.version_id,
        version.name,
        metrics.size,
        metrics.downloads,
        metrics.downloads_month,
        metrics.downloads_week,
        metrics.downloads_day,
        version.date,
        version.tags,
    )


def _version_records(rows: Iterable[Sequence[Any]]) -> tuple[VersionRecord, ...]:
    return tuple(_version_record(row) for row in rows)


def _version_record(row: Sequence[Any]) -> VersionRecord:
    return VersionRecord(
        version_id=str(row[0]),
        name=str(row[1]),
        metrics=VersionMetrics(
            size=int(row[2]),
            downloads=int(row[3]),
            downloads_month=int(row[4]),
            downloads_week=int(row[5]),
            downloads_day=int(row[6]),
        ),
        date=str(row[7]),
        tags="" if row[8] is None else str(row[8]),
    )


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


def _execute_transaction(
    connection: sqlite3.Connection,
    statement: str,
    parameters: Sequence[Any],
) -> None:
    with _transaction(connection):
        connection.execute(statement, parameters)


def _is_retryable(error: sqlite3.Error) -> bool:
    message = str(error).lower()
    return any(fragment in message for fragment in _RETRYABLE_MESSAGES)
