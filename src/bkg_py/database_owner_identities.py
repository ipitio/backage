"""Lazy reconciliation of superseded GitHub owner identities."""

from __future__ import annotations

import sqlite3
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .database_models import OwnerIdentityCleanup, PackageRef
from .database_settings import DatabaseSettings
from .database_support import DatabaseError


class _SqlIdentifier(str):
    """A SQLite identifier quoted before statement substitution."""

    def __new__(cls, value: str) -> _SqlIdentifier:
        if "\x00" in value:
            raise DatabaseError("SQLite identifiers cannot contain NUL")
        quoted = f'"{value.replace(chr(34), chr(34) * 2)}"'
        return str.__new__(cls, quoted)


_SCANS = _SqlIdentifier("bkg_owner_scans")
_SCAN_PACKAGES = _SqlIdentifier("bkg_owner_scan_packages")
_PACKAGE_PUBLICATIONS = _SqlIdentifier("bkg_package_publications")


@dataclass(frozen=True)
class _CleanupContext:
    owner_id: str
    owner: str
    alias_ids: tuple[str, ...]
    orphaned: tuple[PackageRef, ...]


class OwnerIdentityRepositoryMixin(ABC):
    """Add verified owner-ID reconciliation to the SQLite repository."""

    settings: DatabaseSettings

    @abstractmethod
    def ensure_schema(self) -> None:
        """Create or migrate the lazy normalized schema."""

        raise NotImplementedError

    @abstractmethod
    def _run_read(self, operation: Callable[[sqlite3.Connection], Any]) -> Any:
        raise NotImplementedError

    @abstractmethod
    def _run_write(self, operation: Callable[[sqlite3.Connection], Any]) -> Any:
        raise NotImplementedError

    def owner_alias_ids(self, owner_id: str, owner: str) -> tuple[str, ...]:
        """Return persisted IDs superseded by one owner login's current ID."""

        self.ensure_schema()
        packages = _SqlIdentifier(self.settings.packages_table)
        rows = self._run_read(
            lambda connection: connection.execute(
                _sql(
                    """
                select distinct owner_id from {packages}
                where owner = ? collate nocase and owner_id != ?
                order by owner_id
                """,
                    packages=packages,
                ),
                (owner, owner_id),
            ).fetchall()
        )
        return tuple(str(row[0]) for row in rows)

    def retire_owner_aliases(
        self,
        owner_id: str,
        owner: str,
    ) -> OwnerIdentityCleanup:
        """Remove older IDs after GitHub confirms the login's current identity."""

        if not owner_id or not owner:
            raise DatabaseError("owner ID and login are required")
        self.ensure_schema()
        return self._run_write(
            lambda connection: _retire_owner_aliases(
                connection,
                owner_id,
                owner,
                self.settings,
            )
        )


def _retire_owner_aliases(
    connection: sqlite3.Connection,
    owner_id: str,
    owner: str,
    settings: DatabaseSettings,
) -> OwnerIdentityCleanup:
    owners = _SqlIdentifier(settings.owners_table)
    packages = _SqlIdentifier(settings.packages_table)
    versions = _SqlIdentifier(settings.versions_table)
    connection.execute("begin immediate")
    try:
        alias_ids = _alias_ids(connection, owner_id, owner, owners, packages)
        if not alias_ids:
            connection.commit()
            return OwnerIdentityCleanup((), ())

        orphaned = _orphaned_packages(
            connection,
            owner_id,
            owner,
            packages,
        )
        _delete_alias_rows(
            connection,
            _CleanupContext(owner_id, owner, alias_ids, orphaned),
            settings,
            (owners, packages, versions),
        )
    except BaseException:
        connection.rollback()
        raise
    connection.commit()
    return OwnerIdentityCleanup(alias_ids, orphaned)


def _alias_ids(
    connection: sqlite3.Connection,
    owner_id: str,
    owner: str,
    owners: _SqlIdentifier,
    packages: _SqlIdentifier,
) -> tuple[str, ...]:
    rows = connection.execute(
        _sql(
            """
        select owner_id from {packages}
        where owner = ? collate nocase and owner_id != ?
        union
        select owner_id from {owners}
        where owner = ? collate nocase and owner_id != ?
        union
        select owner_id from {scans}
        where owner = ? collate nocase and owner_id != ?
        order by owner_id
        """,
            packages=packages,
            owners=owners,
            scans=_SCANS,
        ),
        (owner, owner_id, owner, owner_id, owner, owner_id),
    ).fetchall()
    return tuple(str(row[0]) for row in rows)


def _orphaned_packages(
    connection: sqlite3.Connection,
    owner_id: str,
    owner: str,
    packages: _SqlIdentifier,
) -> tuple[PackageRef, ...]:
    rows = connection.execute(
        _sql(
            """
        select distinct alias.owner_id, alias.owner_type,
               alias.package_type, alias.owner, alias.repo, alias.package
        from {packages} alias
        where alias.owner = ? collate nocase
          and alias.owner_id != ?
          and not exists (
              select 1 from {packages} current
              where current.owner_id = ?
                and current.owner_type = alias.owner_type
                and current.package_type = alias.package_type
                and current.repo = alias.repo
                and current.package = alias.package
          )
        order by alias.owner_type, alias.package_type,
                 alias.repo, alias.package, alias.owner_id
        """,
            packages=packages,
        ),
        (owner, owner_id, owner_id),
    ).fetchall()
    return tuple(PackageRef(*(str(value) for value in row)) for row in rows)


def _delete_alias_rows(
    connection: sqlite3.Connection,
    context: _CleanupContext,
    settings: DatabaseSettings,
    owner_tables: tuple[_SqlIdentifier, ...],
) -> None:
    for package in context.orphaned:
        legacy_table = (
            f"{settings.versions_table}_{package.owner_type}_{package.package_type}_"
            f"{package.owner}_{package.repo}_{package.package}"
        )
        connection.execute(
            _sql(
                "drop table if exists {legacy}",
                legacy=_SqlIdentifier(legacy_table),
            )
        )
    for table in owner_tables:
        connection.execute(
            _sql(
                """
            delete from {table}
            where owner = ? collate nocase and owner_id != ?
            """,
                table=table,
            ),
            (context.owner, context.owner_id),
        )
    connection.execute(
        _sql(
            """
        delete from {publications}
        where owner = ? collate nocase and owner_id != ?
        """,
            publications=_PACKAGE_PUBLICATIONS,
        ),
        (context.owner, context.owner_id),
    )
    parameters = tuple((alias_id,) for alias_id in context.alias_ids)
    connection.executemany(
        _sql(
            "delete from {scan_packages} where owner_id = ?",
            scan_packages=_SCAN_PACKAGES,
        ),
        parameters,
    )
    connection.executemany(
        _sql("delete from {scans} where owner_id = ?", scans=_SCANS),
        parameters,
    )


def _sql(statement: str, /, **identifiers: _SqlIdentifier) -> str:
    return statement.format_map(identifiers)
