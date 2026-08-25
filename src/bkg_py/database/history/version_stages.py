"""Transactional normalized and legacy package-version stage writes."""

from __future__ import annotations

import sqlite3

from ..models import VersionStage
from ..package import records as packages
from ..support import SqlIdentifier
from ..support import sql as _sql
from ..support import transaction as _transaction
from ..values import legacy_version_values
from . import version_history

_SqlIdentifier = SqlIdentifier


def _table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    return (
        connection.execute(
            """
        select 1 from sqlite_master
        where type = 'table' and name = ?
        limit 1
        """,
            (table_name,),
        ).fetchone()
        is not None
    )


def flush(
    connection: sqlite3.Connection,
    stage: VersionStage,
    publication_pending_at: str | None,
) -> None:
    """Commit a complete version stage and optional publication marker."""

    with _transaction(connection):
        version_history.write_stage(connection, stage)
        if stage.write_legacy and _table_exists(connection, stage.legacy_table):
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
                tuple(legacy_version_values(row) for row in stage.rows),
            )
        if publication_pending_at is not None:
            packages.mark_publication_pending(
                connection,
                stage.package_ref,
                publication_pending_at,
            )
