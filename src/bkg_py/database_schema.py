"""Lazy creation and replacement of normalized SQLite structures."""

from __future__ import annotations

import sqlite3

from .database_support import DatabaseError
from .schema_sql import (
    OWNER_SCAN_SCHEMA_MIGRATIONS,
    PACKAGE_PRIMARY_KEY,
    PACKAGES_TABLE_SQL,
    SCHEMA_SQL,
)

_LEGACY_PACKAGE_PRIMARY_KEY = ("owner_id", "package", "date")
_PACKAGE_COLUMNS = (
    "owner_id",
    "owner_type",
    "package_type",
    "owner",
    "repo",
    "package",
    "downloads",
    "downloads_month",
    "downloads_week",
    "downloads_day",
    "size",
    "date",
)
_PACKAGE_COPY_SQL = """
    insert into {packages} (
        owner_id, owner_type, package_type, owner, repo, package,
        downloads, downloads_month, downloads_week, downloads_day, size, date
    )
    select
        owner_id, owner_type, package_type, owner, repo, package,
        downloads, downloads_month, downloads_week, downloads_day, size, date
    from {legacy}
"""


class _SqlIdentifier(str):
    def __new__(cls, value: str) -> _SqlIdentifier:
        if "\x00" in value:
            raise DatabaseError("SQLite identifiers cannot contain NUL")
        quoted = f'"{value.replace(chr(34), chr(34) * 2)}"'
        return str.__new__(cls, quoted)


def ensure(
    connection: sqlite3.Connection,
    owners_table: str,
    packages_table: str,
    versions_table: str,
) -> None:
    """Create missing structures and lazily replace recognized old shapes."""

    owners = _SqlIdentifier(owners_table)
    packages = _SqlIdentifier(packages_table)
    versions = _SqlIdentifier(versions_table)
    statements = tuple(
        _sql(
            statement,
            owners=owners,
            packages=packages,
            versions=versions,
        )
        for statement in SCHEMA_SQL
    )
    for statement in statements:
        connection.execute(statement)
    package_table_migrated = _migrate_package_primary_key(
        connection,
        packages_table,
        packages,
    )
    if package_table_migrated:
        for statement in statements:
            connection.execute(statement)

    owner_scan_columns = {
        str(row[1])
        for row in connection.execute('pragma table_info("bkg_owner_scans")')
    }
    for column, statement in OWNER_SCAN_SCHEMA_MIGRATIONS:
        if column not in owner_scan_columns:
            connection.execute(statement)


def _migrate_package_primary_key(
    connection: sqlite3.Connection,
    table_name: str,
    packages: _SqlIdentifier,
) -> bool:
    table_info = connection.execute(f"pragma table_info({packages})").fetchall()
    primary_key = tuple(
        str(row[1])
        for row in sorted(table_info, key=lambda row: int(row[5]) or len(table_info))
        if int(row[5]) > 0
    )
    if primary_key == PACKAGE_PRIMARY_KEY:
        return False
    if primary_key != _LEGACY_PACKAGE_PRIMARY_KEY:
        raise DatabaseError(
            f"unsupported primary key for package table {table_name}: "
            f"{', '.join(primary_key) or 'none'}"
        )

    columns = {str(row[1]) for row in table_info}
    missing_columns = tuple(
        column for column in _PACKAGE_COLUMNS if column not in columns
    )
    if missing_columns:
        raise DatabaseError(
            f"cannot rekey package table {table_name}; missing columns: "
            f"{', '.join(missing_columns)}"
        )

    legacy_name = f"{table_name}__bkg_legacy_primary_key"
    if _table_exists(connection, legacy_name):
        raise DatabaseError(
            f"cannot rekey package table {table_name}; temporary table exists"
        )
    legacy = _SqlIdentifier(legacy_name)
    previous_count = _row_count(connection, packages)
    connection.execute(
        _sql(
            "alter table {packages} rename to {legacy}",
            packages=packages,
            legacy=legacy,
        )
    )
    connection.execute(_sql(PACKAGES_TABLE_SQL, packages=packages))
    connection.execute(
        _sql(
            _PACKAGE_COPY_SQL,
            packages=packages,
            legacy=legacy,
        )
    )
    current_count = _row_count(connection, packages)
    if current_count != previous_count:
        raise DatabaseError(
            f"package table rekey copied {current_count} of {previous_count} rows"
        )
    connection.execute(_sql("drop table {legacy}", legacy=legacy))
    return True


def _row_count(connection: sqlite3.Connection, table: _SqlIdentifier) -> int:
    row = connection.execute(
        _sql("select count(*) from {table}", table=table)
    ).fetchone()
    if row is None:
        raise DatabaseError("package table count returned no row")
    return int(row[0])


def _sql(statement: str, /, **identifiers: _SqlIdentifier) -> str:
    return statement.format_map(identifiers)


def _table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    return (
        connection.execute(
            "select 1 from sqlite_master where type = 'table' and name = ? limit 1",
            (table_name,),
        ).fetchone()
        is not None
    )
