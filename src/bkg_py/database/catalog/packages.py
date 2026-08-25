"""Rotation-independent package catalog storage and reconciliation."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable

from ..models import (
    PackageCatalogPath,
    PackageCatalogStatus,
    PackageInventory,
    PackageRecord,
    PackageRef,
)
from ..support import DatabaseError, SqlIdentifier
from ..support import transaction as _transaction
from ..values import package_values

CATALOG_TABLE = "bkg_package_catalog"
CATALOG_STATE_TABLE = "bkg_package_catalog_state"
_SCAN_PACKAGES_TABLE = "bkg_owner_scan_packages"


_SqlIdentifier = SqlIdentifier


def status(connection: sqlite3.Connection) -> PackageCatalogStatus | None:
    """Return committed catalog state, or None before the lazy seed finishes."""

    row = connection.execute(
        f"""
        select source_revision, initialized_at, source_owners,
               source_repositories, source_packages
        from "{CATALOG_STATE_TABLE}"
        where singleton = 1
        """
    ).fetchone()
    if row is None:
        return None
    return PackageCatalogStatus(
        source_revision=str(row[0]),
        initialized_at=str(row[1]),
        source_inventory=PackageInventory(
            owners=int(row[2]),
            repositories=int(row[3]),
            packages=int(row[4]),
        ),
        inventory=inventory(connection),
        resolved_packages=_resolved_count(connection),
    )


def is_ready(connection: sqlite3.Connection) -> bool:
    """Return whether an atomic catalog seed has committed."""

    return (
        connection.execute(
            f'select 1 from "{CATALOG_STATE_TABLE}" where singleton = 1',
        ).fetchone()
        is not None
    )


def initialize(
    connection: sqlite3.Connection,
    packages_table: str,
    paths: Iterable[PackageCatalogPath],
    source_revision: str,
    initialized_at: str,
) -> PackageCatalogStatus:
    """Atomically seed tracked paths and enrich them from normalized rows."""

    if not source_revision:
        raise DatabaseError("package catalog source revision is required")
    if not initialized_at:
        raise DatabaseError("package catalog initialization date is required")
    unique_paths = tuple(dict.fromkeys(paths))
    source_inventory = _path_inventory(unique_paths)
    packages = _SqlIdentifier(packages_table)
    with _transaction(connection):
        connection.execute(f'delete from "{CATALOG_TABLE}"')
        connection.executemany(
            f"""
            insert into "{CATALOG_TABLE}" (owner, repo, package)
            values (?, ?, ?)
            """,
            ((path.owner, path.repo, path.package) for path in unique_paths),
        )
        connection.execute(
            f"""
            insert into "{CATALOG_TABLE}" (
                owner, repo, package, owner_id, owner_type, package_type,
                observed_at
            )
            select owner, repo, package, owner_id, owner_type, package_type, date
            from (
                select history.owner, history.repo, history.package,
                       coalesce(history.owner_id, '') as owner_id,
                       history.owner_type, history.package_type, history.date,
                       row_number() over (
                           partition by history.owner, history.repo, history.package
                           order by history.date desc, history.owner_id,
                                    history.package_type
                       ) as position
                from {packages} history
                join "{CATALOG_TABLE}" catalog
                  on catalog.owner = history.owner
                 and catalog.repo = history.repo
                 and catalog.package = history.package
            ) latest
            where position = 1
            on conflict(owner, repo, package) do update set
                owner_id = excluded.owner_id,
                owner_type = excluded.owner_type,
                package_type = excluded.package_type,
                observed_at = excluded.observed_at
            """
        )
        connection.execute(
            f"""
            insert into "{CATALOG_STATE_TABLE}" (
                singleton, source_revision, initialized_at, source_owners,
                source_repositories, source_packages
            ) values (1, ?, ?, ?, ?, ?)
            on conflict(singleton) do update set
                source_revision = excluded.source_revision,
                initialized_at = excluded.initialized_at,
                source_owners = excluded.source_owners,
                source_repositories = excluded.source_repositories,
                source_packages = excluded.source_packages
            """,
            (
                source_revision,
                initialized_at,
                source_inventory.owners,
                source_inventory.repositories,
                source_inventory.packages,
            ),
        )
    initialized = status(connection)
    if initialized is None:
        raise DatabaseError("package catalog initialization did not commit")
    return initialized


def inventory(connection: sqlite3.Connection) -> PackageInventory:
    """Count current generated owner, repository, and package paths."""

    row = connection.execute(
        f"""
        select
            (select count(*) from (
                select owner from "{CATALOG_TABLE}" group by owner
            )),
            (select count(*) from (
                select owner, repo from "{CATALOG_TABLE}"
                group by owner, repo
            )),
            (select count(*) from "{CATALOG_TABLE}")
        """
    ).fetchone()
    if row is None:
        raise DatabaseError("package catalog inventory returned no row")
    return PackageInventory(*(int(value) for value in row))


def upsert_package(connection: sqlite3.Connection, record: PackageRecord) -> None:
    """Record one successfully persisted package in the current catalog."""

    _upsert(
        connection,
        record.package_ref,
        record.date,
    )


def retire_package(connection: sqlite3.Connection, package: PackageRef) -> None:
    """Remove one authoritative package identity and generated path."""

    connection.execute(
        f"""
        delete from "{CATALOG_TABLE}"
        where (
            owner_id = ? and package_type = ? and repo = ? and package = ?
        ) or (
            owner = ? and repo = ? and package = ?
        )
        """,
        (
            package.owner_id,
            package.package_type,
            package.repo,
            package.package,
            package.owner,
            package.repo,
            package.package,
        ),
    )


def retire_owner(connection: sqlite3.Connection, owner: str) -> None:
    """Remove every catalog path belonging to an unavailable owner."""

    connection.execute(
        f'delete from "{CATALOG_TABLE}" where owner = ?',
        (owner,),
    )


def retire_owner_aliases(
    connection: sqlite3.Connection,
    owner_id: str,
    owner: str,
    alias_ids: tuple[str, ...],
    alias_owners: tuple[str, ...],
) -> None:
    """Remove resolved aliases and tree-only paths under superseded logins."""

    parameters: list[str] = [owner_id, owner, owner, owner_id]
    conditions = [
        """
        (owner_id != '' and (
            (owner_id = ? and owner != ? collate binary)
            or (owner = ? collate nocase and owner_id != ?)
        ))
        """
    ]
    resolved_alias_ids = tuple(value for value in alias_ids if value)
    if resolved_alias_ids:
        conditions.append(f"owner_id in ({_placeholders(len(resolved_alias_ids))})")
        parameters.extend(resolved_alias_ids)
    superseded_owners = tuple(value for value in alias_owners if value != owner)
    if superseded_owners:
        conditions.append(f"owner in ({_placeholders(len(superseded_owners))})")
        parameters.extend(superseded_owners)
    connection.execute(
        f'delete from "{CATALOG_TABLE}" where {" or ".join(conditions)}',
        parameters,
    )


def reconcile_owner_scan(
    connection: sqlite3.Connection,
    owner_id: str,
    owner: str,
    marker: str,
    observed_at: str,
) -> tuple[PackageCatalogPath, ...]:
    """Apply one complete owner listing and return extra paths it retired."""

    rows = connection.execute(
        f"""
        select catalog.owner, catalog.repo, catalog.package
        from "{CATALOG_TABLE}" catalog
        where (catalog.owner_id = ? or (
            catalog.owner_id = '' and catalog.owner = ?
        ))
          and not exists (
              select 1 from "{_SCAN_PACKAGES_TABLE}" observed
              where observed.owner_id = ? and observed.marker = ?
                and observed.repo = catalog.repo
                and observed.package = catalog.package
          )
        order by catalog.owner, catalog.repo, catalog.package
        """,
        (owner_id, owner, owner_id, marker),
    ).fetchall()
    removed = tuple(PackageCatalogPath(*(str(value) for value in row)) for row in rows)
    connection.executemany(
        f"""
        delete from "{CATALOG_TABLE}"
        where owner = ? and repo = ? and package = ?
        """,
        ((path.owner, path.repo, path.package) for path in removed),
    )
    observed = connection.execute(
        f"""
        select owner_type, package_type, repo, package
        from "{_SCAN_PACKAGES_TABLE}"
        where owner_id = ? and marker = ?
        order by owner_type, package_type, repo, package
        """,
        (owner_id, marker),
    ).fetchall()
    for row in observed:
        _upsert(
            connection,
            PackageRef(
                owner_id,
                str(row[0]),
                str(row[1]),
                owner,
                str(row[2]),
                str(row[3]),
            ),
            observed_at,
        )
    return removed


def _upsert(
    connection: sqlite3.Connection,
    package: PackageRef,
    observed_at: str,
) -> None:
    identity = package_values(package)
    connection.execute(
        f"""
        delete from "{CATALOG_TABLE}"
        where owner_id = ? and package_type = ? and repo = ? and package = ?
          and owner != ? collate binary
        """,
        (
            package.owner_id,
            package.package_type,
            package.repo,
            package.package,
            package.owner,
        ),
    )
    connection.execute(
        f"""
        insert into "{CATALOG_TABLE}" (
            owner_id, owner_type, package_type, owner, repo, package, observed_at
        ) values (?, ?, ?, ?, ?, ?, ?)
        on conflict(owner, repo, package) do update set
            owner_id = excluded.owner_id,
            owner_type = excluded.owner_type,
            package_type = excluded.package_type,
            observed_at = excluded.observed_at
        """,
        (*identity, observed_at),
    )


def _resolved_count(connection: sqlite3.Connection) -> int:
    row = connection.execute(
        f"select count(*) from \"{CATALOG_TABLE}\" where owner_id != ''",
    ).fetchone()
    if row is None:
        raise DatabaseError("package catalog resolved count returned no row")
    return int(row[0])


def _path_inventory(paths: tuple[PackageCatalogPath, ...]) -> PackageInventory:
    owners = {path.owner for path in paths}
    repositories = {(path.owner, path.repo) for path in paths}
    return PackageInventory(len(owners), len(repositories), len(paths))


def _placeholders(values: int) -> str:
    return ",".join("?" for _value in range(values))
