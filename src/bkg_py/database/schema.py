"""Lazy creation and replacement of normalized SQLite structures."""

from __future__ import annotations

import sqlite3

from . import package_history, version_history
from .schema_sql import OWNER_SCAN_SCHEMA_MIGRATIONS, SCHEMA_SQL
from .support import SqlIdentifier
from .support import sql as _sql

_SqlIdentifier = SqlIdentifier


def ensure(
    connection: sqlite3.Connection,
    owners_table: str,
    packages_table: str,
    versions_table: str,
) -> None:
    """Create missing structures and lazily replace recognized old shapes."""

    owners = _SqlIdentifier(owners_table)
    statements = tuple(
        _sql(
            statement,
            owners=owners,
        )
        for statement in SCHEMA_SQL
    )
    for statement in statements:
        connection.execute(statement)

    owner_scan_columns = {
        str(row[1])
        for row in connection.execute('pragma table_info("bkg_owner_scans")')
    }
    for column, statement in OWNER_SCAN_SCHEMA_MIGRATIONS:
        if column not in owner_scan_columns:
            connection.execute(statement)
    version_history.ensure(connection, versions_table)
    package_history.ensure(connection, packages_table)
