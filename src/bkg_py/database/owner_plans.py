"""Current-batch package work planning for one owner."""

from __future__ import annotations

import sqlite3

from . import batch_progress
from . import packages as package_records
from .models import (
    OwnerRefreshPlan,
    OwnerRefreshSelection,
    OwnerScanPackage,
    OwnerScanWorkSelection,
    PackageRef,
)
from .support import DatabaseError

_PACKAGE_PUBLICATIONS = '"bkg_package_publications"'
_BATCH_PROGRESS = batch_progress.TABLE


def _identifier(value: str) -> str:
    if "\x00" in value:
        raise DatabaseError("SQLite identifiers cannot contain NUL")
    return f'"{value.replace(chr(34), chr(34) * 2)}"'


def packages_needing_refresh(
    connection: sqlite3.Connection,
    packages_table: str,
    selection: OwnerScanWorkSelection,
) -> tuple[OwnerScanPackage, ...]:
    """Select observed packages needing data refresh or file publication."""

    return tuple(
        package
        for package in selection.packages
        if _package_needs_refresh(
            connection,
            packages_table,
            PackageRef(
                selection.owner_id,
                package.owner_type,
                package.package_type,
                selection.owner,
                package.repo,
                package.package,
            ),
            selection.since,
            selection.batch_marker,
        )
    )


def _package_needs_refresh(
    connection: sqlite3.Connection,
    packages_table: str,
    package: PackageRef,
    since: str,
    batch_marker: str,
) -> bool:
    if batch_marker and not batch_progress.completed(
        connection,
        package,
        batch_marker,
    ):
        return True
    return package_records.needs_refresh(
        connection,
        packages_table,
        package,
        since,
    )


def owner_refresh_plan(
    connection: sqlite3.Connection,
    packages_table: str,
    selection: OwnerRefreshSelection,
) -> OwnerRefreshPlan:
    """Return package work and whether current data permits direct recovery."""

    packages = _identifier(packages_table)
    rows = connection.execute(
        f"""
        select current.owner_type, current.package_type,
               current.repo, current.package, max(current.date),
               max(case when pending.owner_id is null then 0 else 1 end),
               max(
                   case
                       when current.owner = ? and current.date >= ?
                            and pending.owner_id is null
                       then 1 else 0
                   end
               ),
               max(
                   case
                       when current.owner = ? and current.date >= ?
                       then 1 else 0
                   end
               ),
               max(case when progress.batch_marker = ? then 1 else 0 end)
        from {packages} current
        left join {_PACKAGE_PUBLICATIONS} pending
          on pending.owner_id = current.owner_id
         and pending.owner_type = current.owner_type
         and pending.package_type = current.package_type
         and pending.owner = current.owner
         and pending.repo = current.repo
         and pending.package = current.package
        left join {_BATCH_PROGRESS} progress
          on progress.owner_id = current.owner_id
         and progress.owner_type = current.owner_type
         and progress.package_type = current.package_type
         and progress.owner = current.owner
         and progress.repo = current.repo
         and progress.package = current.package
        where current.owner_id = ?
        group by current.owner_type, current.package_type,
                 current.repo, current.package
        order by max(current.date), current.owner_type, current.package_type,
                 current.repo, current.package
        """,
        (
            selection.owner,
            selection.since,
            selection.owner,
            selection.since,
            selection.batch_marker,
            selection.owner_id,
        ),
    ).fetchall()

    work = tuple(
        OwnerScanPackage(*(str(value) for value in row[:4]))
        for row in rows
        if str(row[4]) < selection.since
        or bool(row[5])
        or (bool(selection.batch_marker) and not bool(row[8]))
    )
    return OwnerRefreshPlan(
        any(bool(row[6]) for row in rows),
        work,
        any(bool(row[7]) for row in rows),
    )
