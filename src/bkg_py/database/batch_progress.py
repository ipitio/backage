"""Durable per-package completion state for the active batch marker."""

from __future__ import annotations

import sqlite3

from .models import PackageRef
from .support import DatabaseError
from .values import package_values

TABLE = '"bkg_package_batch_progress"'
_PUBLICATIONS = '"bkg_package_publications"'


def _identifier(value: str) -> str:
    if "\x00" in value:
        raise DatabaseError("SQLite identifiers cannot contain NUL")
    return f'"{value.replace(chr(34), chr(34) * 2)}"'


def bootstrap(
    connection: sqlite3.Connection,
    packages_table: str,
    batch_marker: str,
    since: str,
) -> None:
    """Seed completion for a batch deployed before marker tracking existed."""

    packages = _identifier(packages_table)
    connection.execute(
        f"""
        insert into {TABLE} (
            owner_id, owner_type, package_type, owner, repo, package,
            batch_marker, completed_at
        )
        select current.owner_id, current.owner_type, current.package_type,
               current.owner, current.repo, current.package, ?, max(current.date)
        from {packages} current
        where current.owner_id is not null
          and current.owner_id != ''
          and not exists (
              select 1 from {_PUBLICATIONS} pending
              where pending.owner_id = current.owner_id
                and pending.owner_type = current.owner_type
                and pending.package_type = current.package_type
                and pending.owner = current.owner
                and pending.repo = current.repo
                and pending.package = current.package
          )
        group by current.owner_id, current.owner_type, current.package_type,
                 current.owner, current.repo, current.package
        having max(current.date) >= ?
        on conflict(owner_id, owner_type, package_type, owner, repo, package)
        do update set batch_marker = excluded.batch_marker,
                      completed_at = excluded.completed_at
        """,
        (batch_marker, since),
    )


def completed(
    connection: sqlite3.Connection,
    package: PackageRef,
    batch_marker: str,
) -> bool:
    """Return whether one package completed the active batch generation."""

    if not batch_marker:
        return False
    row = connection.execute(
        f"""
        select 1 from {TABLE}
        where owner_id = ? and owner_type = ? and package_type = ?
          and owner = ? and repo = ? and package = ? and batch_marker = ?
        limit 1
        """,
        (*package_values(package), batch_marker),
    ).fetchone()
    return row is not None


def mark_completed(
    connection: sqlite3.Connection,
    package: PackageRef,
    batch_marker: str,
    completed_at: str,
) -> None:
    """Record successful publication in the active batch generation."""

    if not batch_marker:
        return
    connection.execute(
        f"""
        insert into {TABLE} (
            owner_id, owner_type, package_type, owner, repo, package,
            batch_marker, completed_at
        ) values (?, ?, ?, ?, ?, ?, ?, ?)
        on conflict(owner_id, owner_type, package_type, owner, repo, package)
        do update set batch_marker = excluded.batch_marker,
                      completed_at = excluded.completed_at
        """,
        (*package_values(package), batch_marker, completed_at),
    )


def retire_package(connection: sqlite3.Connection, package: PackageRef) -> None:
    """Remove progress for a package no longer present in normalized state."""

    connection.execute(
        f"""
        delete from {TABLE}
        where owner_id = ? and owner_type = ? and package_type = ?
          and owner = ? and repo = ? and package = ?
        """,
        package_values(package),
    )


def retire_owner(connection: sqlite3.Connection, owner: str) -> None:
    """Remove progress for an owner retired from normalized state."""

    connection.execute(f"delete from {TABLE} where owner = ?", (owner,))
