"""SQLite package rows and recoverable generated-file publication state."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Generator
from contextlib import contextmanager

from . import batch_progress, catalog, package_history, version_history
from .models import PackageInventory, PackageRecord, PackageRef
from .support import DatabaseError
from .values import package_values

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
def _transaction(connection: sqlite3.Connection) -> Generator[None]:
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
    versions_table: str,
    record: PackageRecord,
    *,
    mark_pending: bool,
) -> None:
    """Write one package row, prune obsolete partial stages, and mark files."""

    package = record.package_ref
    with _transaction(connection):
        package_history.write_observation(connection, record)
        _delete_unpaired_versions(
            connection,
            packages_table,
            versions_table,
            package,
        )
        catalog.upsert_package(connection, record)
        if mark_pending:
            mark_publication_pending(connection, package, record.date)


def _delete_unpaired_versions(
    connection: sqlite3.Connection,
    packages_table: str,
    versions_table: str,
    package: PackageRef,
) -> None:
    packages = _SqlIdentifier(packages_table)
    package_identity = package_values(package)
    if _table_exists(connection, versions_table):
        versions = _SqlIdentifier(versions_table)
        connection.execute(
            _sql(
                """
                delete from {versions}
                where owner_id = ? and owner_type = ? and package_type = ?
                  and owner = ? and repo = ? and package = ?
                  and not exists (
                      select 1 from {packages}
                      where owner_id = ? and owner_type = ? and package_type = ?
                        and owner = ? and repo = ? and package = ?
                        and {packages}.date = {versions}.date
                  )
                """,
                packages=packages,
                versions=versions,
            ),
            (*package_identity, *package_identity),
        )
    version_history.delete_unpaired(connection, packages_table, package)


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


def inventory(
    connection: sqlite3.Connection,
    packages_table: str,
    check_stop: Callable[[], None],
) -> PackageInventory:
    """Count distinct package paths, owner IDs, and owner repositories."""

    rows = connection.execute(
        _sql(
            """
            select owner_id, repo
            from (
                select owner_id, owner, repo, package
                from {packages}
                group by owner_id, owner, repo, package
            )
            order by owner_id, repo
            """,
            packages=_SqlIdentifier(packages_table),
        )
    )
    owner_count = 0
    repository_count = 0
    package_count = 0
    previous_owner: str | None = None
    previous_repository: tuple[str, str] | None = None

    for index, row in enumerate(rows):
        if index % 1024 == 0:
            check_stop()
        owner_id = str(row[0])
        repository = (owner_id, str(row[1]))
        package_count += 1
        if owner_id != previous_owner:
            owner_count += 1
            previous_owner = owner_id
        if repository != previous_repository:
            repository_count += 1
            previous_repository = repository

    return PackageInventory(
        owners=owner_count,
        repositories=repository_count,
        packages=package_count,
    )


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
        version_history.retire_package(connection, package)
        table_names: list[str] = []
        if _table_exists(connection, packages_table):
            table_names.append(packages_table)
        if _table_exists(connection, versions_table):
            table_names.append(versions_table)
        for table_name in table_names:
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
        batch_progress.retire_package(connection, package)
        catalog.retire_package(connection, package)


def _table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    return (
        connection.execute(
            "select 1 from sqlite_master where type = 'table' and name = ? limit 1",
            (table_name,),
        ).fetchone()
        is not None
    )


def retire_owner_publications(connection: sqlite3.Connection, owner: str) -> None:
    """Delete publication markers for one retired owner."""

    connection.execute(
        'delete from "bkg_package_publications" where owner = ?',
        (owner,),
    )
