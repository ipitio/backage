"""Bounded SQLite storage measurements for database finalization."""

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any, Literal, cast

from ..history import version_history
from ..support import DatabaseError, SqlIdentifier

DatabaseObjectKind = Literal["table", "index", "internal"]
_DATE_BUCKET_LIMIT = 32
_DATE_FIELD_COUNT = 2
_METRICS_TABLE = "bkg_database_metrics"
_OBJECT_FIELD_COUNT = 4
_METRICS_UPSERT = f"""
    insert into "{_METRICS_TABLE}" (
        sample_date, run_count, physical_bytes, logical_bytes,
        page_size, page_count, freelist_pages, package_rows, version_rows,
        package_rows_written, version_rows_written,
        maximum_pre_rotation_bytes, rotation_count, rotation_archive_bytes,
        snapshot_bytes, object_bytes_json, package_rows_by_date_json,
        version_rows_by_date_json
    ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    on conflict(sample_date) do update set
        run_count = "{_METRICS_TABLE}".run_count + excluded.run_count,
        physical_bytes = excluded.physical_bytes,
        logical_bytes = excluded.logical_bytes,
        page_size = excluded.page_size,
        page_count = excluded.page_count,
        freelist_pages = excluded.freelist_pages,
        package_rows = excluded.package_rows,
        version_rows = excluded.version_rows,
        package_rows_written = (
            "{_METRICS_TABLE}".package_rows_written
            + excluded.package_rows_written
        ),
        version_rows_written = (
            "{_METRICS_TABLE}".version_rows_written
            + excluded.version_rows_written
        ),
        maximum_pre_rotation_bytes = max(
            "{_METRICS_TABLE}".maximum_pre_rotation_bytes,
            excluded.maximum_pre_rotation_bytes
        ),
        rotation_count = (
            "{_METRICS_TABLE}".rotation_count + excluded.rotation_count
        ),
        rotation_archive_bytes = (
            "{_METRICS_TABLE}".rotation_archive_bytes
            + excluded.rotation_archive_bytes
        ),
        snapshot_bytes = excluded.snapshot_bytes,
        object_bytes_json = excluded.object_bytes_json,
        package_rows_by_date_json = excluded.package_rows_by_date_json,
        version_rows_by_date_json = excluded.version_rows_by_date_json
"""


_SqlIdentifier = SqlIdentifier


@dataclass(frozen=True)
class DatabaseObjectBytes:
    """Storage occupied by one schema object or bounded object family."""

    kind: DatabaseObjectKind
    name: str
    objects: int
    bytes: int


@dataclass(frozen=True)
class DatabaseDateRows:
    """Rows associated with one date or the bounded older-date aggregate."""

    date: str
    rows: int


@dataclass(frozen=True)
class DatabasePageMetrics:
    """Physical and logical page accounting for one SQLite snapshot."""

    physical_bytes: int
    logical_bytes: int
    page_size: int
    page_count: int
    freelist_pages: int


@dataclass(frozen=True)
class DatabaseStorageMetrics:
    """One checkpointed view of SQLite storage and normalized history."""

    pages: DatabasePageMetrics
    package_rows: int
    version_rows: int
    objects: tuple[DatabaseObjectBytes, ...]
    package_rows_by_date: tuple[DatabaseDateRows, ...]
    version_rows_by_date: tuple[DatabaseDateRows, ...]


@dataclass(frozen=True)
class DatabaseWriteCounts:
    """Normalized rows written by the current application process."""

    package_rows: int = 0
    version_rows: int = 0


class DatabaseWriteTracker:
    """Thread-safe process-local normalized row-write accounting."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._package_rows = 0
        self._version_rows = 0

    def add_package_rows(self, rows: int) -> None:
        """Record successful normalized package writes."""

        with self._lock:
            self._package_rows += rows

    def add_version_rows(self, rows: int) -> None:
        """Record successful normalized version writes."""

        with self._lock:
            self._version_rows += rows

    def counts(self) -> DatabaseWriteCounts:
        """Return one consistent counter snapshot."""

        with self._lock:
            return DatabaseWriteCounts(self._package_rows, self._version_rows)


@dataclass(frozen=True)
class DatabaseMetricSample:  # pylint: disable=too-many-instance-attributes
    """One compact daily finalization sample persisted in SQLite."""

    sample_date: str
    run_count: int
    storage: DatabaseStorageMetrics
    writes: DatabaseWriteCounts
    maximum_pre_rotation_bytes: int
    rotation_count: int
    rotation_archive_bytes: int
    snapshot_bytes: int


def capture(
    connection: sqlite3.Connection,
    path: Path,
    packages_table: str,
    versions_table: str,
) -> DatabaseStorageMetrics:
    """Capture bounded storage and row-distribution measurements."""

    page_size = _pragma_int(connection, "page_size")
    page_count = _pragma_int(connection, "page_count")
    freelist_pages = _pragma_int(connection, "freelist_count")
    package_dates = _date_rows(connection, packages_table)
    version_dates = _date_rows(connection, version_history.VERSION_HISTORY_VIEW)
    pages = DatabasePageMetrics(
        physical_bytes=_file_size(path),
        logical_bytes=max(0, page_count - freelist_pages) * page_size,
        page_size=page_size,
        page_count=page_count,
        freelist_pages=freelist_pages,
    )
    return DatabaseStorageMetrics(
        pages=pages,
        package_rows=sum(item.rows for item in package_dates),
        version_rows=sum(item.rows for item in version_dates),
        objects=_object_bytes(connection, versions_table),
        package_rows_by_date=package_dates,
        version_rows_by_date=version_dates,
    )


def record(connection: sqlite3.Connection, sample: DatabaseMetricSample) -> None:
    """Upsert one daily sample while accumulating same-day run writes."""

    storage = sample.storage
    pages = storage.pages
    connection.execute(
        _METRICS_UPSERT,
        (
            sample.sample_date,
            sample.run_count,
            pages.physical_bytes,
            pages.logical_bytes,
            pages.page_size,
            pages.page_count,
            pages.freelist_pages,
            storage.package_rows,
            storage.version_rows,
            sample.writes.package_rows,
            sample.writes.version_rows,
            sample.maximum_pre_rotation_bytes,
            sample.rotation_count,
            sample.rotation_archive_bytes,
            sample.snapshot_bytes,
            _object_json(storage.objects),
            _date_json(storage.package_rows_by_date),
            _date_json(storage.version_rows_by_date),
        ),
    )


def load_samples(connection: sqlite3.Connection) -> tuple[DatabaseMetricSample, ...]:
    """Load daily samples in chronological order."""

    rows = connection.execute(
        f"""
        select sample_date, run_count, physical_bytes, logical_bytes,
               page_size, page_count, freelist_pages, package_rows,
               version_rows, package_rows_written, version_rows_written,
               maximum_pre_rotation_bytes, rotation_count,
               rotation_archive_bytes, snapshot_bytes, object_bytes_json,
               package_rows_by_date_json, version_rows_by_date_json
        from "{_METRICS_TABLE}"
        order by sample_date
        """
    ).fetchall()
    return tuple(_sample(row) for row in rows)


def _pragma_int(connection: sqlite3.Connection, name: str) -> int:
    row = connection.execute(f"pragma {name}").fetchone()
    if row is None:
        raise DatabaseError(f"SQLite pragma {name} returned no row")
    return int(row[0])


def _date_rows(
    connection: sqlite3.Connection,
    table_name: str,
) -> tuple[DatabaseDateRows, ...]:
    table = _SqlIdentifier(table_name)
    rows = connection.execute(
        f"""
        select date, count(*)
        from {table}
        group by date
        order by date desc
        limit {_DATE_BUCKET_LIMIT}
        """
    ).fetchall()
    dated = [DatabaseDateRows(str(row[0]), int(row[1])) for row in rows]
    if len(dated) == _DATE_BUCKET_LIMIT:
        oldest = dated[-1].date
        older = connection.execute(
            f"select count(*) from {table} where date < ?",
            (oldest,),
        ).fetchone()
        if older is not None and int(older[0]) > 0:
            dated.append(DatabaseDateRows(f"<{oldest}", int(older[0])))
    dated.reverse()
    return tuple(dated)


def _object_bytes(
    connection: sqlite3.Connection,
    versions_table: str,
) -> tuple[DatabaseObjectBytes, ...]:
    prefix = f"{versions_table}_"
    try:
        rows = connection.execute(
            """
            with object_pages as (
                select name, sum(pgsize) as bytes
                from dbstat
                group by name
            ), classified as (
                select
                    case
                        when master.type = 'table'
                         and master.name != ?
                         and substr(master.name, 1, length(?)) = ?
                            then 'table'
                        when master.type = 'index'
                         and substr(master.tbl_name, 1, length(?)) = ?
                            then 'index'
                        else coalesce(master.type, 'internal')
                    end as kind,
                    case
                        when master.type = 'table'
                         and master.name != ?
                         and substr(master.name, 1, length(?)) = ?
                            then 'legacy-version-tables'
                        when master.type = 'index'
                         and substr(master.tbl_name, 1, length(?)) = ?
                            then 'legacy-version-indexes'
                        else object_pages.name
                    end as object_name,
                    object_pages.bytes as bytes
                from object_pages
                left join sqlite_master master on master.name = object_pages.name
            )
            select kind, object_name, count(*), sum(bytes)
            from classified
            group by kind, object_name
            order by sum(bytes) desc, object_name
            """,
            (
                versions_table,
                prefix,
                prefix,
                prefix,
                prefix,
                versions_table,
                prefix,
                prefix,
                prefix,
                prefix,
            ),
        ).fetchall()
    except sqlite3.OperationalError as error:
        if "dbstat" in str(error).lower():
            return ()
        raise
    return tuple(
        DatabaseObjectBytes(
            _object_kind(str(row[0])),
            str(row[1]),
            int(row[2]),
            int(row[3]),
        )
        for row in rows
    )


def _object_kind(value: str) -> DatabaseObjectKind:
    if value == "table":
        return "table"
    if value == "index":
        return "index"
    return "internal"


def _object_json(items: tuple[DatabaseObjectBytes, ...]) -> str:
    return json.dumps(
        [[item.kind, item.name, item.objects, item.bytes] for item in items],
        separators=(",", ":"),
    )


def _date_json(items: tuple[DatabaseDateRows, ...]) -> str:
    return json.dumps(
        [[item.date, item.rows] for item in items],
        separators=(",", ":"),
    )


def _sample(row: sqlite3.Row | tuple[Any, ...]) -> DatabaseMetricSample:
    values = tuple(row)
    pages = DatabasePageMetrics(
        physical_bytes=int(values[2]),
        logical_bytes=int(values[3]),
        page_size=int(values[4]),
        page_count=int(values[5]),
        freelist_pages=int(values[6]),
    )
    storage = DatabaseStorageMetrics(
        pages=pages,
        package_rows=int(values[7]),
        version_rows=int(values[8]),
        objects=_load_objects(str(values[15])),
        package_rows_by_date=_load_dates(str(values[16])),
        version_rows_by_date=_load_dates(str(values[17])),
    )
    return DatabaseMetricSample(
        sample_date=str(values[0]),
        run_count=int(values[1]),
        storage=storage,
        writes=DatabaseWriteCounts(int(values[9]), int(values[10])),
        maximum_pre_rotation_bytes=int(values[11]),
        rotation_count=int(values[12]),
        rotation_archive_bytes=int(values[13]),
        snapshot_bytes=int(values[14]),
    )


def _load_objects(value: str) -> tuple[DatabaseObjectBytes, ...]:
    raw = _load_records(
        value,
        "database object metrics",
        _OBJECT_FIELD_COUNT,
    )
    try:
        return tuple(
            DatabaseObjectBytes(
                _object_kind(str(item[0])),
                str(item[1]),
                int(item[2]),
                int(item[3]),
            )
            for item in raw
        )
    except (TypeError, ValueError) as error:
        raise DatabaseError("invalid database object metrics") from error


def _load_dates(value: str) -> tuple[DatabaseDateRows, ...]:
    raw = _load_records(value, "database date metrics", _DATE_FIELD_COUNT)
    try:
        return tuple(DatabaseDateRows(str(item[0]), int(item[1])) for item in raw)
    except (TypeError, ValueError) as error:
        raise DatabaseError("invalid database date metrics") from error


def _load_list(value: str, label: str) -> list[Any]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as error:
        raise DatabaseError(f"invalid {label}") from error
    if not isinstance(parsed, list):
        raise DatabaseError(f"invalid {label}")
    return cast(list[Any], parsed)


def _load_records(value: str, label: str, fields: int) -> list[list[Any]]:
    records: list[list[Any]] = []
    for item in _load_list(value, label):
        if not isinstance(item, list):
            raise DatabaseError(f"invalid {label}")
        fields_record = cast(list[Any], item)
        if len(fields_record) != fields:
            raise DatabaseError(f"invalid {label}")
        records.append(fields_record)
    return records


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0
