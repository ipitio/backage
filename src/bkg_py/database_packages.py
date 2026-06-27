"""SQLite package rows and recoverable generated-file publication state."""

from __future__ import annotations

import sqlite3
from collections.abc import Generator
from contextlib import contextmanager

from .database_models import PackageRecord, PackageRef
from .database_support import DatabaseError
from .database_values import package_values

_PUBLICATION_UPSERT = """
    insert into "bkg_package_publications" (
        owner_id, owner_type, package_type, owner, repo, package, updated_at
    ) values (?, ?, ?, ?, ?, ?, ?)
    on conflict(owner_id, owner_type, package_type, owner, repo, package)
    do update set updated_at = excluded.updated_at
"""
_PUBLICATION_SELECT = """
    select 1 from "bkg_package_publications"
    where owner_id = ? and owner_type = ? and package_type = ?
      and owner = ? and repo = ? and package = ?
    limit 1
"""
_PUBLICATION_DELETE = """
    delete from "bkg_package_publications"
    where owner_id = ? and owner_type = ? and package_type = ?
      and owner = ? and repo = ? and package = ?
"""


class _SqlIdentifier(str):
    """A SQLite identifier quoted before statement construction."""

    def __new__(cls, value: str) -> _SqlIdentifier:
        if "\x00" in value:
            raise DatabaseError("SQLite identifiers cannot contain NUL")
        quoted = f'"{value.replace(chr(34), chr(34) * 2)}"'
        return str.__new__(cls, quoted)


def _sql(statement: str, /, **identifiers: _SqlIdentifier) -> str:
    return statement.format_map(identifiers)


@contextmanager
def _transaction(connection: sqlite3.Connection) -> Generator[None, None, None]:
    connection.execute("begin immediate")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    connection.commit()


def write(
    connection: sqlite3.Connection,
    packages_table: str,
    record: PackageRecord,
    *,
    mark_pending: bool,
) -> None:
    """Write one package row and optionally mark its files stale."""

    package = record.package_ref
    with _transaction(connection):
        connection.execute(
            _sql(
                """
                insert or replace into {packages} (
                    owner_id, owner_type, package_type, owner, repo, package,
                    downloads, downloads_month, downloads_week, downloads_day,
                    size, date
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                packages=_SqlIdentifier(packages_table),
            ),
            (
                *package_values(package),
                record.downloads,
                record.downloads_month,
                record.downloads_week,
                record.downloads_day,
                record.size,
                record.date,
            ),
        )
        if mark_pending:
            mark_publication_pending(connection, package, record.date)


def updated_since(
    connection: sqlite3.Connection,
    packages_table: str,
    package: PackageRef,
    since: str,
) -> bool:
    """Return whether a package row belongs to the current batch."""

    row = connection.execute(
        _sql(
            """
            select 1 from {packages}
            where owner_id = ? and owner_type = ? and package_type = ?
              and owner = ? and repo = ? and package = ? and date >= ?
            limit 1
            """,
            packages=_SqlIdentifier(packages_table),
        ),
        (*package_values(package), since),
    ).fetchone()
    return row is not None


def maximum_downloads(
    connection: sqlite3.Connection,
    packages_table: str,
    package: PackageRef,
) -> int:
    """Return the largest stored package download total."""

    row = connection.execute(
        _sql(
            """
            select max(downloads) from {packages}
            where owner_id = ? and package = ?
            """,
            packages=_SqlIdentifier(packages_table),
        ),
        (package.owner_id, package.package),
    ).fetchone()
    if row is None or row[0] is None:
        return -1
    return int(row[0])


def mark_publication_pending(
    connection: sqlite3.Connection,
    package: PackageRef,
    updated_at: str,
) -> None:
    """Upsert one generated-file publication marker."""

    connection.execute(
        _PUBLICATION_UPSERT,
        (*package_values(package), updated_at),
    )


def mark_publication_pending_transaction(
    connection: sqlite3.Connection,
    package: PackageRef,
    updated_at: str,
) -> None:
    """Upsert one publication marker in its own transaction."""

    with _transaction(connection):
        mark_publication_pending(connection, package, updated_at)


def publication_pending(
    connection: sqlite3.Connection,
    package: PackageRef,
) -> bool:
    """Return whether one package's generated files are stale."""

    return (
        connection.execute(
            _PUBLICATION_SELECT,
            package_values(package),
        ).fetchone()
        is not None
    )


def needs_refresh(
    connection: sqlite3.Connection,
    packages_table: str,
    package: PackageRef,
    since: str,
) -> bool:
    """Return whether data or generated files need current-batch work."""

    return not updated_since(
        connection,
        packages_table,
        package,
        since,
    ) or publication_pending(connection, package)


def clear_publication(
    connection: sqlite3.Connection,
    package: PackageRef,
) -> None:
    """Delete one successfully published package marker."""

    connection.execute(_PUBLICATION_DELETE, package_values(package))


def clear_publication_transaction(
    connection: sqlite3.Connection,
    package: PackageRef,
) -> None:
    """Delete one publication marker in its own transaction."""

    with _transaction(connection):
        clear_publication(connection, package)


def retire(
    connection: sqlite3.Connection,
    packages_table: str,
    versions_table: str,
    legacy_table: str,
    package: PackageRef,
) -> None:
    """Delete one package from normalized, legacy, and publication storage."""

    with _transaction(connection):
        connection.execute(
            _sql(
                "drop table if exists {legacy}",
                legacy=_SqlIdentifier(legacy_table),
            )
        )
        for table_name in (packages_table, versions_table):
            connection.execute(
                _sql(
                    """
                    delete from {table}
                    where owner_id = ? and owner_type = ? and package_type = ?
                      and owner = ? and repo = ? and package = ?
                    """,
                    table=_SqlIdentifier(table_name),
                ),
                package_values(package),
            )
        clear_publication(connection, package)


def retire_owner_publications(connection: sqlite3.Connection, owner: str) -> None:
    """Delete publication markers for one retired owner."""

    connection.execute(
        'delete from "bkg_package_publications" where owner = ?',
        (owner,),
    )
