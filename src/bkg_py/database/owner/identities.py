"""Lazy reconciliation of superseded GitHub owner identities."""

import sqlite3
from dataclasses import dataclass

from ..catalog import packages as catalog
from ..history import package_history, version_history
from ..kernel import DatabaseComponent
from ..models import OwnerIdentityCleanup, PackageRef
from ..settings import DatabaseSettings
from ..support import DatabaseError, SqlFragment, SqlIdentifier
from ..support import sql as _sql

_SqlIdentifier = SqlIdentifier


_SCANS = _SqlIdentifier("bkg_owner_scans")
_SCAN_PACKAGES = _SqlIdentifier("bkg_owner_scan_packages")
_PACKAGE_PUBLICATIONS = _SqlIdentifier("bkg_package_publications")
_BATCH_PROGRESS = _SqlIdentifier("bkg_package_batch_progress")


@dataclass(frozen=True)
class _CleanupContext:
    owner_id: str
    owner: str
    alias_ids: tuple[str, ...]
    alias_owners: tuple[str, ...]
    orphaned: tuple[PackageRef, ...]


class OwnerIdentityRepository(DatabaseComponent):
    """Provide verified owner-ID reconciliation."""

    def owner_alias_ids(self, owner_id: str, owner: str) -> tuple[str, ...]:
        """Return persisted IDs superseded by one owner login's current ID."""

        self.ensure_schema()
        packages = _SqlIdentifier(package_history.PACKAGE_HISTORY_VIEW)
        rows = self._run_read(
            lambda connection: connection.execute(
                _sql(
                    """
                select distinct coalesce(owner_id, '') from {packages}
                where owner = ? collate nocase
                  and (owner_id is null or owner_id != ?)
                order by coalesce(owner_id, '')
                """,
                    packages=packages,
                ),
                (owner, owner_id),
            ).fetchall()
        )
        return tuple(str(row[0]) for row in rows)

    def owner_has_aliases(self, owner_id: str, owner: str) -> bool:
        """Return whether stored owner rows differ from the verified identity."""

        self.ensure_schema()
        return self._run_read(
            lambda connection: _has_alias_rows(
                connection,
                owner_id,
                owner,
                self.settings,
            )
        )

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
    packages = _SqlIdentifier(package_history.PACKAGE_HISTORY_VIEW)
    versions = (
        _SqlIdentifier(settings.versions_table)
        if _table_exists(connection, settings.versions_table)
        else None
    )
    connection.execute("begin immediate")
    try:
        alias_ids = _alias_ids(connection, owner_id, owner, owners, packages)
        has_alias_rows = _has_alias_rows_for_tables(
            connection,
            owner_id,
            owner,
            (owners, packages, _SCANS),
        )
        if not alias_ids and not has_alias_rows:
            connection.commit()
            return OwnerIdentityCleanup((), ())

        alias_owners = _alias_owners(
            connection,
            owner_id,
            owner,
            owners,
            packages,
        )
        orphaned = _orphaned_packages(
            connection,
            owner_id,
            owner,
            packages,
        )
        owner_tables = [owners]
        if _table_exists(connection, settings.packages_table):
            owner_tables.append(_SqlIdentifier(settings.packages_table))
        if versions is not None:
            owner_tables.append(versions)
        _delete_alias_rows(
            connection,
            _CleanupContext(owner_id, owner, alias_ids, alias_owners, orphaned),
            settings,
            tuple(owner_tables),
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
        select coalesce(owner_id, '') from {packages}
        where owner = ? collate nocase
          and (owner_id is null or owner_id != ?)
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


def _alias_owners(
    connection: sqlite3.Connection,
    owner_id: str,
    owner: str,
    owners: _SqlIdentifier,
    packages: _SqlIdentifier,
) -> tuple[str, ...]:
    rows = connection.execute(
        _sql(
            """
        select owner from {packages} where {alias_condition}
        union
        select owner from {owners} where {alias_condition}
        union
        select owner from {scans} where {alias_condition}
        order by owner
        """,
            packages=packages,
            owners=owners,
            scans=_SCANS,
            alias_condition=_alias_condition(),
        ),
        (owner_id, owner, owner, owner_id) * 3,
    ).fetchall()
    return tuple(str(row[0]) for row in rows)


def _has_alias_rows(
    connection: sqlite3.Connection,
    owner_id: str,
    owner: str,
    settings: DatabaseSettings,
) -> bool:
    owners = _SqlIdentifier(settings.owners_table)
    packages = _SqlIdentifier(package_history.PACKAGE_HISTORY_VIEW)
    return _has_alias_rows_for_tables(
        connection,
        owner_id,
        owner,
        (owners, packages, _SCANS),
    )


def _has_alias_rows_for_tables(
    connection: sqlite3.Connection,
    owner_id: str,
    owner: str,
    tables: tuple[_SqlIdentifier, _SqlIdentifier, _SqlIdentifier],
) -> bool:
    owners, packages, scans = tables
    row = connection.execute(
        _sql(
            """
        select 1 from {packages}
        where {alias_condition}
        union all
        select 1 from {owners}
        where {alias_condition}
        union all
        select 1 from {scans}
        where {alias_condition}
        limit 1
        """,
            packages=packages,
            owners=owners,
            scans=scans,
            alias_condition=_alias_condition(),
        ),
        (owner_id, owner, owner, owner_id) * 3,
    ).fetchone()
    return row is not None


def _orphaned_packages(
    connection: sqlite3.Connection,
    owner_id: str,
    owner: str,
    packages: _SqlIdentifier,
) -> tuple[PackageRef, ...]:
    rows = connection.execute(
        _sql(
            """
        select distinct coalesce(alias.owner_id, ''), alias.owner_type,
               alias.package_type, alias.owner, alias.repo, alias.package
        from {packages} alias
        where {alias_condition}
          and (
              alias.owner != ? collate binary
              or not exists (
                  select 1 from {packages} current
                  where current.owner_id = ?
                    and current.owner = ? collate binary
                    and current.owner_type = alias.owner_type
                    and current.package_type = alias.package_type
                    and current.repo = alias.repo
                    and current.package = alias.package
              )
          )
        order by alias.owner_type, alias.package_type,
                 alias.repo, alias.package, alias.owner_id
        """,
            packages=packages,
            alias_condition=_alias_condition("alias"),
        ),
        (owner_id, owner, owner, owner_id, owner, owner_id, owner),
    ).fetchall()
    return tuple(PackageRef(*(str(value) for value in row)) for row in rows)


def _delete_alias_rows(
    connection: sqlite3.Connection,
    context: _CleanupContext,
    settings: DatabaseSettings,
    owner_tables: tuple[_SqlIdentifier, ...],
) -> None:
    version_history.retire_owner_aliases(
        connection,
        context.owner_id,
        context.owner,
    )
    catalog.retire_owner_aliases(
        connection,
        context.owner_id,
        context.owner,
        context.alias_ids,
        context.alias_owners,
    )
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
            where {alias_condition}
            """,
                table=table,
                alias_condition=_alias_condition(),
            ),
            (context.owner_id, context.owner, context.owner, context.owner_id),
        )
    connection.execute(
        _sql(
            """
        delete from {publications}
        where {alias_condition}
        """,
            publications=_PACKAGE_PUBLICATIONS,
            alias_condition=_alias_condition(),
        ),
        (context.owner_id, context.owner, context.owner, context.owner_id),
    )
    connection.execute(
        _sql(
            """
        delete from {progress}
        where {alias_condition}
        """,
            progress=_BATCH_PROGRESS,
            alias_condition=_alias_condition(),
        ),
        (context.owner_id, context.owner, context.owner, context.owner_id),
    )
    connection.execute(
        _sql(
            """
        delete from {scan_packages}
        where exists (
            select 1 from {scans}
            where {scans}.owner_id = {scan_packages}.owner_id
              and {scans}.marker = {scan_packages}.marker
              and {alias_condition}
        )
        """,
            scan_packages=_SCAN_PACKAGES,
            scans=_SCANS,
            alias_condition=_alias_condition(_SCANS),
        ),
        (context.owner_id, context.owner, context.owner, context.owner_id),
    )
    connection.execute(
        _sql(
            """
        delete from {scans}
        where {alias_condition}
        """,
            scans=_SCANS,
            alias_condition=_alias_condition(),
        ),
        (context.owner_id, context.owner, context.owner, context.owner_id),
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


def _alias_condition(alias: str = "") -> SqlFragment:
    qualifier = f"{alias}." if alias else ""
    return SqlFragment(
        f"(({qualifier}owner_id = ? and {qualifier}owner != ? collate binary) "
        f"or ({qualifier}owner = ? collate nocase "
        f"and ({qualifier}owner_id is null or {qualifier}owner_id != ?)))"
    )


def _table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    return (
        connection.execute(
            "select 1 from sqlite_master where type = 'table' and name = ? limit 1",
            (table_name,),
        ).fetchone()
        is not None
    )
