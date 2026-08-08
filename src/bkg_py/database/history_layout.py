"""Measure a normalized version-history layout without changing live data."""

from __future__ import annotations

import hashlib
import sqlite3
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from .support import DatabaseError
from .version_history import (
    DATA_SCHEMA_SQL,
    PACKAGE_IDENTITIES_TABLE,
    VERSION_IDENTITIES_TABLE,
    VERSION_OBSERVATIONS_TABLE,
)

_PACKAGE_IDENTITIES = PACKAGE_IDENTITIES_TABLE
_VERSION_IDENTITIES = VERSION_IDENTITIES_TABLE
_VERSION_OBSERVATIONS = VERSION_OBSERVATIONS_TABLE
_REQUIRED_COLUMNS = frozenset(
    {
        "owner_id",
        "owner_type",
        "package_type",
        "owner",
        "repo",
        "package",
        "id",
        "name",
        "size",
        "downloads",
        "downloads_month",
        "downloads_week",
        "downloads_day",
        "date",
        "tags",
    }
)
_IDENTITY_JOIN = f"""
    join "{_PACKAGE_IDENTITIES}" packages
      on packages.owner_id = legacy.owner_id
     and packages.owner_type = legacy.owner_type
     and packages.package_type = legacy.package_type
     and packages.owner = legacy.owner
     and packages.repo = legacy.repo
     and packages.package = legacy.package
"""


class _SqlIdentifier(str):
    """A SQLite identifier quoted before statement construction."""

    def __new__(cls, value: str) -> _SqlIdentifier:
        if "\x00" in value:
            raise DatabaseError("SQLite identifiers cannot contain NUL")
        return str.__new__(cls, f'"{value.replace(chr(34), chr(34) * 2)}"')


@dataclass(frozen=True)
class HistoryQueryMeasurement:
    """Equivalent source and candidate timings for one history read shape."""

    name: str
    rows: int
    source_seconds: float
    candidate_seconds: float


@dataclass(frozen=True)
class HistoryLayoutMeasurement:  # pylint: disable=too-many-instance-attributes
    """Storage, migration, and query measurements for one source database."""

    source_file_bytes: int
    source_history_bytes: int
    candidate_file_bytes: int
    candidate_history_bytes: int
    version_observations: int
    package_identities: int
    version_identities: int
    migration_seconds: float
    queries: tuple[HistoryQueryMeasurement, ...]

    @property
    def history_bytes_saved(self) -> int:
        """Return candidate savings across version-history SQLite objects."""

        return self.source_history_bytes - self.candidate_history_bytes

    @property
    def history_reduction_percent(self) -> float:
        """Return the percentage reduction across measured history objects."""

        if self.source_history_bytes <= 0:
            return 0.0
        return 100.0 * self.history_bytes_saved / self.source_history_bytes


def measure_history_layout(
    source_path: Path,
    candidate_path: Path,
    *,
    versions_table: str = "versions",
) -> HistoryLayoutMeasurement:
    """Build and measure a normalized copy while leaving the source read-only."""

    source = source_path.resolve()
    candidate = candidate_path.resolve()
    if not source.is_file():
        raise DatabaseError(f"history benchmark source does not exist: {source}")
    if candidate.exists():
        raise DatabaseError(f"history benchmark output already exists: {candidate}")
    candidate.parent.mkdir(parents=True, exist_ok=True)

    table = _SqlIdentifier(versions_table)
    source_file_bytes = source.stat().st_size
    with _read_only_connection(source) as source_connection:
        _validate_source(source_connection, table)
        source_history_bytes = _table_object_bytes(
            source_connection,
            versions_table,
        )

    started_at = time.perf_counter()
    with sqlite3.connect(candidate, uri=True) as connection:
        _configure_candidate(connection)
        connection.execute("attach database ? as source", (_read_only_uri(source),))
        _create_candidate(connection)
        _copy_history(connection, table)
        connection.commit()
        migration_seconds = time.perf_counter() - started_at
        queries = _measure_queries(connection, table)
        counts = _candidate_counts(connection)
        connection.execute("detach database source")
        connection.execute("vacuum")
        candidate_history_bytes = _tables_object_bytes(
            connection,
            (_PACKAGE_IDENTITIES, _VERSION_IDENTITIES, _VERSION_OBSERVATIONS),
        )

    return HistoryLayoutMeasurement(
        source_file_bytes=source_file_bytes,
        source_history_bytes=source_history_bytes,
        candidate_file_bytes=candidate.stat().st_size,
        candidate_history_bytes=candidate_history_bytes,
        version_observations=counts[2],
        package_identities=counts[0],
        version_identities=counts[1],
        migration_seconds=migration_seconds,
        queries=queries,
    )


def _read_only_connection(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(_read_only_uri(path), uri=True)


def _read_only_uri(path: Path) -> str:
    return f"{path.as_uri()}?mode=ro"


def _validate_source(
    connection: sqlite3.Connection,
    table: _SqlIdentifier,
) -> None:
    columns = {str(row[1]) for row in connection.execute(f"pragma table_info({table})")}
    missing = sorted(_REQUIRED_COLUMNS - columns)
    if missing:
        raise DatabaseError(
            "history benchmark source is missing version columns: " + ", ".join(missing)
        )


def _configure_candidate(connection: sqlite3.Connection) -> None:
    connection.execute("pragma foreign_keys = on")
    connection.execute("pragma journal_mode = off")
    connection.execute("pragma synchronous = off")
    connection.execute("pragma temp_store = memory")
    connection.execute("pragma cache_size = -500000")


def _create_candidate(connection: sqlite3.Connection) -> None:
    for statement in DATA_SCHEMA_SQL:
        connection.execute(statement.replace("if not exists ", ""))


def _copy_history(
    connection: sqlite3.Connection,
    table: _SqlIdentifier,
) -> None:
    connection.execute(
        f"""
        insert into "{_PACKAGE_IDENTITIES}" (
            owner_id, owner_type, package_type, owner, repo, package
        )
        select owner_id, owner_type, package_type, owner, repo, package
        from source.{table}
        group by owner_id, owner_type, package_type, owner, repo, package
        """
    )
    connection.execute(
        f"""
        insert into "{_VERSION_IDENTITIES}" (
            package_key, external_id, name
        )
        select packages.package_key, legacy.id, legacy.name
        from source.{table} legacy
        {_IDENTITY_JOIN}
        group by packages.package_key, legacy.id, legacy.name
        """
    )
    connection.execute(
        f"""
        insert into "{_VERSION_OBSERVATIONS}" (
            version_key, date, size, downloads, downloads_month,
            downloads_week, downloads_day, tags
        )
        select versions.version_key, legacy.date, legacy.size,
               legacy.downloads, legacy.downloads_month,
               legacy.downloads_week, legacy.downloads_day, legacy.tags
        from source.{table} legacy
        {_IDENTITY_JOIN}
        join "{_VERSION_IDENTITIES}" versions
          on versions.package_key = packages.package_key
         and versions.external_id = legacy.id
         and versions.name = legacy.name
        """
    )


def _candidate_counts(connection: sqlite3.Connection) -> tuple[int, int, int]:
    return (
        _row_count(connection, _PACKAGE_IDENTITIES),
        _row_count(connection, _VERSION_IDENTITIES),
        _row_count(connection, _VERSION_OBSERVATIONS),
    )


def _row_count(connection: sqlite3.Connection, table: str) -> int:
    row = connection.execute(f'select count(*) from "{table}"').fetchone()
    if row is None:
        raise DatabaseError(f"history benchmark could not count {table}")
    return int(row[0])


def _measure_queries(
    connection: sqlite3.Connection,
    table: _SqlIdentifier,
) -> tuple[HistoryQueryMeasurement, ...]:
    package = _largest_group(
        connection,
        f"""
        select owner_id, owner_type, package_type, owner, repo, package
        from source.{table}
        group by owner_id, owner_type, package_type, owner, repo, package
        order by count(*) desc, owner_id, repo, package
        limit 1
        """,
    )
    owner = _largest_group(
        connection,
        f"""
        select owner_id
        from source.{table}
        group by owner_id
        order by count(*) desc, owner_id
        limit 1
        """,
    )
    if package is None or owner is None:
        return ()
    return (
        _measure_equivalent_query(
            connection,
            "largest-package",
            _legacy_package_query(table),
            _candidate_package_query(),
            package,
        ),
        _measure_equivalent_query(
            connection,
            "largest-owner",
            _legacy_owner_query(table),
            _candidate_owner_query(),
            owner,
        ),
    )


def _largest_group(
    connection: sqlite3.Connection,
    statement: str,
) -> tuple[object, ...] | None:
    row = connection.execute(statement).fetchone()
    return None if row is None else tuple(row)


def _measure_equivalent_query(
    connection: sqlite3.Connection,
    name: str,
    source_sql: str,
    candidate_sql: str,
    parameters: Sequence[object],
) -> HistoryQueryMeasurement:
    source_started = time.perf_counter()
    source_rows, source_digest = _query_digest(
        connection.execute(source_sql, parameters)
    )
    source_seconds = time.perf_counter() - source_started
    candidate_started = time.perf_counter()
    candidate_rows, candidate_digest = _query_digest(
        connection.execute(candidate_sql, parameters)
    )
    candidate_seconds = time.perf_counter() - candidate_started
    if (candidate_rows, candidate_digest) != (source_rows, source_digest):
        raise DatabaseError(f"history benchmark {name} query output differs")
    return HistoryQueryMeasurement(
        name,
        source_rows,
        source_seconds,
        candidate_seconds,
    )


def _query_digest(rows: Iterable[sqlite3.Row]) -> tuple[int, str]:
    digest = hashlib.sha256()
    count = 0
    for row in rows:
        for value in row:
            encoded = "" if value is None else str(value)
            digest.update(encoded.encode("utf-8"))
            digest.update(b"\x00")
        digest.update(b"\x1e")
        count += 1
    return count, digest.hexdigest()


def _legacy_package_query(table: _SqlIdentifier) -> str:
    return f"""
        select id, name, size, downloads, downloads_month, downloads_week,
               downloads_day, date, tags
        from source.{table}
        where owner_id = ? and owner_type = ? and package_type = ?
          and owner = ? and repo = ? and package = ?
        order by id, name, date
    """


def _candidate_package_query() -> str:
    return f"""
        select versions.external_id, versions.name, observations.size,
               observations.downloads, observations.downloads_month,
               observations.downloads_week, observations.downloads_day,
               observations.date, observations.tags
        from "{_PACKAGE_IDENTITIES}" packages
        join "{_VERSION_IDENTITIES}" versions
          on versions.package_key = packages.package_key
        join "{_VERSION_OBSERVATIONS}" observations
          on observations.version_key = versions.version_key
        where packages.owner_id = ? and packages.owner_type = ?
          and packages.package_type = ? and packages.owner = ?
          and packages.repo = ? and packages.package = ?
        order by versions.external_id, versions.name, observations.date
    """


def _legacy_owner_query(table: _SqlIdentifier) -> str:
    return f"""
        select owner_id, owner_type, package_type, owner, repo, package,
               id, name, size, downloads, downloads_month, downloads_week,
               downloads_day, date, tags
        from source.{table}
        where owner_id = ?
        order by owner_type, package_type, owner, repo, package, id, name, date
    """


def _candidate_owner_query() -> str:
    return f"""
        select packages.owner_id, packages.owner_type, packages.package_type,
               packages.owner, packages.repo, packages.package,
               versions.external_id, versions.name, observations.size,
               observations.downloads, observations.downloads_month,
               observations.downloads_week, observations.downloads_day,
               observations.date, observations.tags
        from "{_PACKAGE_IDENTITIES}" packages
        join "{_VERSION_IDENTITIES}" versions
          on versions.package_key = packages.package_key
        join "{_VERSION_OBSERVATIONS}" observations
          on observations.version_key = versions.version_key
        where packages.owner_id = ?
        order by packages.owner_type, packages.package_type, packages.owner,
                 packages.repo, packages.package, versions.external_id,
                 versions.name, observations.date
    """


def _table_object_bytes(connection: sqlite3.Connection, table: str) -> int:
    row = connection.execute(
        """
        select coalesce(sum(pgsize), 0)
        from dbstat
        where name = ?
           or name in (
               select name from sqlite_master
               where type = 'index' and tbl_name = ?
           )
        """,
        (table, table),
    ).fetchone()
    return 0 if row is None else int(row[0])


def _tables_object_bytes(
    connection: sqlite3.Connection,
    tables: Sequence[str],
) -> int:
    return sum(_table_object_bytes(connection, table) for table in tables)
