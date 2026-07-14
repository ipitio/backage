"""Snapshot-consistent package and owner work planning."""

from __future__ import annotations

import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from .models import PackageWorkItem, PackageWorkPlan
from .support import DatabaseError


@dataclass(frozen=True)
class PackagePlanSelection:
    """Tables and active batch used to build one package work plan."""

    packages_table: str
    owners_table: str
    since: str
    batch_marker: str = ""


class _SqlIdentifier(str):
    """A SQLite identifier quoted before statement construction."""

    def __new__(cls, value: str) -> _SqlIdentifier:
        if "\x00" in value:
            raise DatabaseError("SQLite identifiers cannot contain NUL")
        quoted = f'"{value.replace(chr(34), chr(34) * 2)}"'
        return str.__new__(cls, quoted)


def load(
    connection: sqlite3.Connection,
    selection: PackagePlanSelection,
) -> PackageWorkPlan:
    """Build current package work and owner ordering from one read snapshot."""

    packages = _SqlIdentifier(selection.packages_table)
    owners = _SqlIdentifier(selection.owners_table)
    with _read_snapshot(connection):
        package_rows = connection.execute(
            _sql(
                """
                select owner_id, owner, repo, package, max(date) as max_date
                from {packages}
                group by owner_id, owner, repo, package
                order by max_date asc
                """,
                packages=packages,
            )
        ).fetchall()
        completed_rows = _completed_rows(
            connection,
            packages,
            selection.since,
            selection.batch_marker,
        )
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
            (selection.since,),
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
            (selection.since,),
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


def _completed_rows(
    connection: sqlite3.Connection,
    packages: _SqlIdentifier,
    since: str,
    batch_marker: str,
) -> list[sqlite3.Row] | list[tuple[Any, ...]]:
    if batch_marker:
        statement = _sql(
            """
            select current.owner_id, current.owner, current.repo,
                   current.package, max(current.date) as max_date
            from {packages} current
            join bkg_package_batch_progress progress
              on progress.owner_id = current.owner_id
             and progress.owner_type = current.owner_type
             and progress.package_type = current.package_type
             and progress.owner = current.owner
             and progress.repo = current.repo
             and progress.package = current.package
             and progress.batch_marker = ?
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
            order by max_date asc
            """,
            packages=packages,
        )
        parameters = (batch_marker,)
    else:
        statement = _sql(
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
        )
        parameters = (since,)
    return connection.execute(statement, parameters).fetchall()


def _sql(statement: str, /, **identifiers: _SqlIdentifier) -> str:
    return statement.format_map(identifiers)


def _package_work_item(row: tuple[Any, ...] | sqlite3.Row) -> PackageWorkItem:
    return PackageWorkItem(*(str(value) for value in row))


@contextmanager
def _read_snapshot(connection: sqlite3.Connection) -> Generator[None, None, None]:
    connection.execute("begin")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    connection.commit()
