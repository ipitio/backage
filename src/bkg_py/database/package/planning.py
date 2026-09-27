"""Snapshot-consistent package and owner work planning."""

import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass

from ..models import PackageWorkPlan
from ..support import SqlIdentifier
from ..support import sql as _sql
from ..values import package_work_item


@dataclass(frozen=True)
class PackagePlanSelection:
    """Tables and active batch used to build one package work plan."""

    packages_table: str
    owners_table: str
    since: str
    batch_marker: str = ""


_SqlIdentifier = SqlIdentifier


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
                with current as (
                    select owner_id, owner_type, package_type, owner, repo, package,
                           max(date) as latest_date
                    from {packages}
                    group by owner_id, owner_type, package_type, owner, repo, package
                )
                select current.owner_id, current.owner_type, current.package_type,
                       current.owner, current.repo, current.package,
                       current.latest_date,
                       case
                           when pending.owner_id is not null then 0
                           when ? != '' then coalesce(progress.batch_marker = ?, 0)
                           else current.latest_date >= ?
                       end as completed
                from current
                left join bkg_package_batch_progress progress
                  on progress.owner_id = current.owner_id
                 and progress.owner_type = current.owner_type
                 and progress.package_type = current.package_type
                 and progress.owner = current.owner
                 and progress.repo = current.repo
                 and progress.package = current.package
                left join bkg_package_publications pending
                  on pending.owner_id = current.owner_id
                 and pending.owner_type = current.owner_type
                 and pending.package_type = current.package_type
                 and pending.owner = current.owner
                 and pending.repo = current.repo
                 and pending.package = current.package
                order by current.latest_date, current.owner_id, current.owner_type,
                         current.package_type, current.owner, current.repo,
                         current.package
                """,
                packages=packages,
            ),
            (selection.batch_marker, selection.batch_marker, selection.since),
        ).fetchall()
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

    all_packages = tuple(package_work_item(row[:-1]) for row in package_rows)
    completed = tuple(
        item for item, row in zip(all_packages, package_rows, strict=True) if row[-1]
    )
    completed_refs = {item.package_ref for item in completed}
    return PackageWorkPlan(
        all_packages,
        completed,
        tuple(item for item in all_packages if item.package_ref not in completed_refs),
        tuple(str(row[0]) for row in owner_rows),
        tuple(str(row[0]) for row in empty_owner_rows),
    )


@contextmanager
def _read_snapshot(connection: sqlite3.Connection) -> Generator[None]:
    connection.execute("begin")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    connection.commit()
