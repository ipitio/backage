"""Transactional normalized and legacy package-version stage writes."""

from __future__ import annotations

import sqlite3
from collections.abc import Generator
from contextlib import contextmanager

from . import packages, version_history
from .models import VersionStage
from .support import DatabaseError
from .values import legacy_version_values


class _SqlIdentifier(str):
    """A SQLite identifier quoted before statement construction."""

    def __new__(cls, value: str) -> _SqlIdentifier:
        if "\x00" in value:
            raise DatabaseError("SQLite identifiers cannot contain NUL")
        quoted = f'"{value.replace(chr(34), chr(34) * 2)}"'
        return str.__new__(cls, quoted)


def _sql(statement: str, /, **identifiers: _SqlIdentifier) -> str:
    return statement.format_map(identifiers)


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


@contextmanager
def _transaction(connection: sqlite3.Connection) -> Generator[None]:
    connection.execute("begin immediate")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    connection.commit()


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
