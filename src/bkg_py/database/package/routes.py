"""Stable repository-scoped routes observed lazily during owner discovery."""

import sqlite3

from ..catalog import packages as catalog
from ..models import PackageRef
from ..support import DatabaseError, SqlIdentifier
from ..support import sql as _sql
from ..values import package_values

TABLE = SqlIdentifier("bkg_package_routes")


def source_package_id(connection: sqlite3.Connection, package: PackageRef) -> str:
    """Look up a discovered route without inferring one from historical rows."""

    row = connection.execute(
        _sql(
            """
        select source_package_id from {routes}
        where owner_id = ? and owner_type = ? and package_type = ?
          and owner = ? and repo = ? and package = ?
        """,
            routes=TABLE,
        ),
        package_values(package),
    ).fetchone()
    return "" if row is None else str(row[0])


def observe(
    connection: sqlite3.Connection, package: PackageRef, package_id: str
) -> None:
    """Store a positive GitHub package ID in the listing page transaction."""

    if not package_id.isascii() or not package_id.isdigit() or int(package_id) <= 0:
        raise DatabaseError("repository-scoped package ID must be positive")
    connection.execute(
        _sql(
            """
        insert into {routes} (
            owner_id, owner_type, package_type, owner, repo, package, source_package_id
        ) values (?, ?, ?, ?, ?, ?, ?)
        on conflict(owner_id, owner_type, package_type, owner, repo, package)
        do update set source_package_id = excluded.source_package_id
        """,
            routes=TABLE,
        ),
        (*package_values(package), package_id),
    )


def retire_package(connection: sqlite3.Connection, package: PackageRef) -> None:
    """Remove a route when its package is retired or opted out."""

    connection.execute(
        _sql(
            """
        delete from {routes}
        where owner_id = ? and owner_type = ? and package_type = ?
          and owner = ? and repo = ? and package = ?
        """,
            routes=TABLE,
        ),
        package_values(package),
    )


def retire_owner(connection: sqlite3.Connection, owner: str) -> None:
    """Remove routes belonging to a verified unavailable owner."""

    connection.execute(
        _sql("delete from {routes} where owner = ?", routes=TABLE), (owner,)
    )


def prune_orphans(connection: sqlite3.Connection) -> None:
    """Keep live routes across rotation, but not abandoned routing metadata."""

    if not catalog.is_ready(connection):
        return
    connection.execute(
        """
        delete from bkg_package_routes
        where not exists (
            select 1 from bkg_package_catalog catalog
            where catalog.owner = bkg_package_routes.owner
              and catalog.repo = bkg_package_routes.repo
              and catalog.package = bkg_package_routes.package
        ) and not exists (
            select 1 from bkg_owner_scan_packages staged
            join bkg_owner_scans scans
              on scans.owner_id = staged.owner_id and scans.marker = staged.marker
            where scans.status = 'running'
              and staged.owner_id = bkg_package_routes.owner_id
              and staged.owner_type = bkg_package_routes.owner_type
              and staged.package_type = bkg_package_routes.package_type
              and staged.repo = bkg_package_routes.repo
              and staged.package = bkg_package_routes.package
        )
        """
    )
