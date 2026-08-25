"""Shared package identities for normalized package and version history."""

from __future__ import annotations

import sqlite3

from ..models import PackageRef
from ..support import DatabaseError
from ..values import package_values

PACKAGE_IDENTITIES_TABLE = "bkg_history_packages"
PACKAGE_IDENTITY_COLUMNS = (
    "owner_id",
    "owner_type",
    "package_type",
    "owner",
    "repo",
    "package",
)
PACKAGE_IDENTITIES_SCHEMA_SQL = f"""
    create table if not exists "{PACKAGE_IDENTITIES_TABLE}" (
        package_key integer primary key,
        owner_id text not null,
        owner_type text not null,
        package_type text not null,
        owner text not null,
        repo text not null,
        package text not null,
        unique ({", ".join(PACKAGE_IDENTITY_COLUMNS)})
    )
"""
_VERSION_IDENTITIES_TABLE = "bkg_history_versions"
_PACKAGE_OBSERVATIONS_TABLE = "bkg_history_package_observations"


def ensure(connection: sqlite3.Connection) -> None:
    """Create the shared package identity table."""

    connection.execute(PACKAGE_IDENTITIES_SCHEMA_SQL)


def package_key(connection: sqlite3.Connection, package: PackageRef) -> int:
    """Return a stable surrogate key, creating the identity when needed."""

    identity = package_values(package)
    connection.execute(
        f"""
        insert into "{PACKAGE_IDENTITIES_TABLE}" (
            {", ".join(PACKAGE_IDENTITY_COLUMNS)}
        ) values (?, ?, ?, ?, ?, ?)
        on conflict({", ".join(PACKAGE_IDENTITY_COLUMNS)}) do nothing
        """,
        identity,
    )
    stored = existing_package_key(connection, package)
    if stored is None:
        raise DatabaseError("package history identity was not persisted")
    return stored


def existing_package_key(
    connection: sqlite3.Connection,
    package: PackageRef,
) -> int | None:
    """Return an existing surrogate key without creating an identity."""

    row = connection.execute(
        f"""
        select package_key from "{PACKAGE_IDENTITIES_TABLE}"
        where owner_id = ? and owner_type = ? and package_type = ?
          and owner = ? and repo = ? and package = ?
        """,
        package_values(package),
    ).fetchone()
    return None if row is None else int(row[0])


def prune_package_key(connection: sqlite3.Connection, key: int) -> None:
    """Remove one package identity only when neither history family uses it."""

    predicates = _retention_predicates(connection)
    connection.execute(
        f"""
        delete from "{PACKAGE_IDENTITIES_TABLE}"
        where package_key = ?
          {"".join(predicates)}
        """,
        (key,),
    )


def prune_identities(connection: sqlite3.Connection) -> None:
    """Remove package identities no longer used by either history family."""

    predicates = _retention_predicates(connection)
    connection.execute(
        f"""
        delete from "{PACKAGE_IDENTITIES_TABLE}"
        where 1 = 1
          {"".join(predicates)}
        """
    )


def _retention_predicates(connection: sqlite3.Connection) -> tuple[str, ...]:
    predicates: list[str] = []
    if _table_exists(connection, _VERSION_IDENTITIES_TABLE):
        predicates.append(
            f"""
          and not exists (
              select 1 from "{_VERSION_IDENTITIES_TABLE}" versions
              where versions.package_key =
                    "{PACKAGE_IDENTITIES_TABLE}".package_key
          )
            """
        )
    if _table_exists(connection, _PACKAGE_OBSERVATIONS_TABLE):
        predicates.append(
            f"""
          and not exists (
              select 1 from "{_PACKAGE_OBSERVATIONS_TABLE}" observations
              where observations.package_key =
                    "{PACKAGE_IDENTITIES_TABLE}".package_key
          )
            """
        )
    return tuple(predicates)


def _table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    return (
        connection.execute(
            "select 1 from sqlite_master where type = 'table' and name = ? limit 1",
            (table_name,),
        ).fetchone()
        is not None
    )
