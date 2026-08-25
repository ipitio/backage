"""Lazy normalized package observations and compatibility reads."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from ..models import PackageRecord
from ..support import DatabaseError, SqlIdentifier
from . import packages as history_packages

PACKAGE_OBSERVATIONS_TABLE = "bkg_history_package_observations"
PACKAGE_HISTORY_STATE_TABLE = "bkg_package_history_state"
PACKAGE_HISTORY_VIEW = "bkg_package_history"
MIGRATION_BATCH_ROWS = 50_000

DATA_SCHEMA_SQL = (
    f"""
    create table if not exists "{PACKAGE_OBSERVATIONS_TABLE}" (
        package_key integer not null references
            "{history_packages.PACKAGE_IDENTITIES_TABLE}" on delete cascade,
        date text not null,
        downloads integer not null,
        downloads_month integer not null,
        downloads_week integer not null,
        downloads_day integer not null,
        size integer not null,
        primary key (package_key, date)
    ) without rowid
    """,
    f"""
    create index if not exists "idx_bkg_history_package_observations_date"
    on "{PACKAGE_OBSERVATIONS_TABLE}" (date)
    """,
)
_STATE_SCHEMA_SQL = f"""
    create table if not exists "{PACKAGE_HISTORY_STATE_TABLE}" (
        singleton integer primary key check (singleton = 1),
        phase text not null check (phase in ('migrating', 'ready')),
        migrated_rows integer not null default 0,
        remaining_rows integer not null default 0,
        updated_at text not null
    )
"""
_STATE_UPSERT_SQL = f"""
    insert into "{PACKAGE_HISTORY_STATE_TABLE}" (
        singleton, phase, migrated_rows, remaining_rows, updated_at
    ) values (1, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
    on conflict(singleton) do update set
        phase = excluded.phase,
        migrated_rows = "{PACKAGE_HISTORY_STATE_TABLE}".migrated_rows
            + excluded.migrated_rows,
        remaining_rows = excluded.remaining_rows,
        updated_at = excluded.updated_at
"""
_RESERVED_NAMES = frozenset(
    {
        history_packages.PACKAGE_IDENTITIES_TABLE,
        PACKAGE_OBSERVATIONS_TABLE,
        PACKAGE_HISTORY_STATE_TABLE,
        PACKAGE_HISTORY_VIEW,
    }
)


_SqlIdentifier = SqlIdentifier


@dataclass(frozen=True)
class PackageHistoryMigration:
    """Progress from one committed delete-after-copy migration batch."""

    migrated_rows: int
    remaining_rows: int

    @property
    def complete(self) -> bool:
        """Return whether the compatibility table has been removed."""

        return self.remaining_rows == 0


def ensure(connection: sqlite3.Connection, legacy_table_name: str) -> None:
    """Create replacement storage and its current compatibility view."""

    if legacy_table_name in _RESERVED_NAMES:
        raise DatabaseError(
            f"package table name is reserved by history storage: {legacy_table_name}"
        )
    history_packages.ensure(connection)
    for statement in (*DATA_SCHEMA_SQL, _STATE_SCHEMA_SQL):
        connection.execute(statement)

    if not _table_exists(connection, legacy_table_name):
        _mark_state(connection, "ready", remaining_rows=0)
        _replace_view(connection, None)
        return
    if _table_has_rows(connection, legacy_table_name):
        _mark_state(
            connection,
            "migrating",
            remaining_rows=_row_count(connection, legacy_table_name),
        )
        _replace_view(connection, legacy_table_name)
        return
    _finish_migration(connection, legacy_table_name)


def write_observation(
    connection: sqlite3.Connection,
    record: PackageRecord,
) -> None:
    """Upsert one package observation without repeating its text identity."""

    package_key = history_packages.package_key(connection, record.package_ref)
    connection.execute(
        f"""
        insert into "{PACKAGE_OBSERVATIONS_TABLE}" (
            package_key, date, downloads, downloads_month,
            downloads_week, downloads_day, size
        ) values (?, ?, ?, ?, ?, ?, ?)
        on conflict(package_key, date) do update set
            downloads = excluded.downloads,
            downloads_month = excluded.downloads_month,
            downloads_week = excluded.downloads_week,
            downloads_day = excluded.downloads_day,
            size = excluded.size
        """,
        (
            package_key,
            record.date,
            record.downloads,
            record.downloads_month,
            record.downloads_week,
            record.downloads_day,
            record.size,
        ),
    )


def migrate_batch(
    connection: sqlite3.Connection,
    legacy_table_name: str,
    row_limit: int,
) -> PackageHistoryMigration:
    """Move one bounded legacy row batch and delete it atomically."""

    if row_limit <= 0:
        raise ValueError("package-history migration row limit must be positive")
    if not _table_exists(connection, legacy_table_name):
        _mark_state(connection, "ready", remaining_rows=0)
        _replace_view(connection, None)
        return PackageHistoryMigration(0, 0)

    legacy = _SqlIdentifier(legacy_table_name)
    connection.execute(
        """
        create temp table if not exists "bkg_package_history_migration_rows" (
            legacy_rowid integer primary key
        ) without rowid
        """
    )
    connection.execute('delete from "bkg_package_history_migration_rows"')
    connection.execute(
        f"""
        insert into "bkg_package_history_migration_rows" (legacy_rowid)
        select rowid from {legacy} order by rowid limit ?
        """,
        (row_limit,),
    )
    migrated_rows = _row_count(connection, "bkg_package_history_migration_rows")
    if migrated_rows == 0:
        _finish_migration(connection, legacy_table_name)
        return PackageHistoryMigration(0, 0)

    _copy_selected_rows(connection, legacy)
    connection.execute(
        f"""
        delete from {legacy}
        where rowid in (
            select legacy_rowid from "bkg_package_history_migration_rows"
        )
        """
    )
    if not _table_has_rows(connection, legacy_table_name):
        _mark_state(
            connection,
            "migrating",
            migrated_rows,
            remaining_rows=0,
        )
        _finish_migration(connection, legacy_table_name)
        return PackageHistoryMigration(migrated_rows, 0)
    remaining_rows = max(0, _state_remaining_rows(connection) - migrated_rows)
    _mark_state(
        connection,
        "migrating",
        migrated_rows,
        remaining_rows=remaining_rows,
    )
    return PackageHistoryMigration(migrated_rows, remaining_rows)


def migrate_remaining(
    connection: sqlite3.Connection,
    legacy_table_name: str,
    *,
    row_limit: int,
) -> int:
    """Finish all retained rows before rotation removes the old table."""

    migrated_rows = 0
    while True:
        progress = migrate_batch(connection, legacy_table_name, row_limit)
        migrated_rows += progress.migrated_rows
        if progress.complete:
            return migrated_rows


def prune_before(connection: sqlite3.Connection, since: str) -> None:
    """Delete replacement observations older than the retained window."""

    connection.execute(
        f'delete from "{PACKAGE_OBSERVATIONS_TABLE}" where date < ?',
        (since,),
    )
    history_packages.prune_identities(connection)


def owner_observation_count(connection: sqlite3.Connection, owner: str) -> int:
    """Count replacement package observations owned by one login."""

    row = connection.execute(
        f"""
        select count(*)
        from "{PACKAGE_OBSERVATIONS_TABLE}" observations
        join "{history_packages.PACKAGE_IDENTITIES_TABLE}" packages
          on packages.package_key = observations.package_key
        where packages.owner = ?
        """,
        (owner,),
    ).fetchone()
    return 0 if row is None else int(row[0])


def _copy_selected_rows(
    connection: sqlite3.Connection,
    legacy: _SqlIdentifier,
) -> None:
    selected_join = (
        'join "bkg_package_history_migration_rows" selected '
        "on selected.legacy_rowid = legacy.rowid"
    )
    columns = history_packages.PACKAGE_IDENTITY_COLUMNS
    identity_expressions = (
        "coalesce(legacy.owner_id, '')",
        *(f"legacy.{column}" for column in columns[1:]),
    )
    connection.execute(
        f"""
        insert into "{history_packages.PACKAGE_IDENTITIES_TABLE}" (
            {", ".join(columns)}
        )
        select {", ".join(identity_expressions)}
        from {legacy} legacy
        {selected_join}
        group by {", ".join(identity_expressions)}
        on conflict({", ".join(columns)}) do nothing
        """
    )
    connection.execute(
        f"""
        insert into "{PACKAGE_OBSERVATIONS_TABLE}" (
            package_key, date, downloads, downloads_month,
            downloads_week, downloads_day, size
        )
        select packages.package_key, legacy.date, legacy.downloads,
               legacy.downloads_month, legacy.downloads_week,
               legacy.downloads_day, legacy.size
        from {legacy} legacy
        {selected_join}
        join "{history_packages.PACKAGE_IDENTITIES_TABLE}" packages
          on packages.owner_id = coalesce(legacy.owner_id, '')
         and packages.owner_type = legacy.owner_type
         and packages.package_type = legacy.package_type
         and packages.owner = legacy.owner
         and packages.repo = legacy.repo
         and packages.package = legacy.package
        on conflict(package_key, date) do nothing
        """
    )


def _replace_view(
    connection: sqlite3.Connection,
    legacy_table_name: str | None,
) -> None:
    connection.execute(f'drop view if exists "{PACKAGE_HISTORY_VIEW}"')
    candidate = _candidate_view_select()
    if legacy_table_name is None:
        connection.execute(f'create view "{PACKAGE_HISTORY_VIEW}" as {candidate}')
        return
    legacy = _SqlIdentifier(legacy_table_name)
    connection.execute(
        f"""
        create view "{PACKAGE_HISTORY_VIEW}" as
        select coalesce(legacy.owner_id, '') as owner_id, legacy.owner_type,
               legacy.package_type,
               legacy.owner, legacy.repo, legacy.package,
               legacy.downloads, legacy.downloads_month,
               legacy.downloads_week, legacy.downloads_day,
               legacy.size, legacy.date
        from {legacy} legacy
        where not exists (
            select 1
            from "{history_packages.PACKAGE_IDENTITIES_TABLE}" packages
            join "{PACKAGE_OBSERVATIONS_TABLE}" observations
              on observations.package_key = packages.package_key
            where packages.owner_id = coalesce(legacy.owner_id, '')
              and packages.owner_type = legacy.owner_type
              and packages.package_type = legacy.package_type
              and packages.owner = legacy.owner
              and packages.repo = legacy.repo
              and packages.package = legacy.package
              and observations.date = legacy.date
        )
        union all
        {candidate}
        """
    )


def _candidate_view_select() -> str:
    return f"""
        select packages.owner_id, packages.owner_type, packages.package_type,
               packages.owner, packages.repo, packages.package,
               observations.downloads, observations.downloads_month,
               observations.downloads_week, observations.downloads_day,
               observations.size, observations.date
        from "{history_packages.PACKAGE_IDENTITIES_TABLE}" packages
        join "{PACKAGE_OBSERVATIONS_TABLE}" observations
          on observations.package_key = packages.package_key
    """


def _finish_migration(
    connection: sqlite3.Connection,
    legacy_table_name: str,
) -> None:
    connection.execute(f'drop view if exists "{PACKAGE_HISTORY_VIEW}"')
    if _table_exists(connection, legacy_table_name):
        connection.execute(f"drop table {_SqlIdentifier(legacy_table_name)}")
    history_packages.prune_identities(connection)
    _mark_state(connection, "ready", remaining_rows=0)
    _replace_view(connection, None)


def _mark_state(
    connection: sqlite3.Connection,
    phase: str,
    migrated_rows: int = 0,
    *,
    remaining_rows: int,
) -> None:
    connection.execute(
        _STATE_UPSERT_SQL,
        (phase, migrated_rows, remaining_rows),
    )


def _state_remaining_rows(connection: sqlite3.Connection) -> int:
    row = connection.execute(
        f"""
        select remaining_rows from "{PACKAGE_HISTORY_STATE_TABLE}"
        where singleton = 1
        """
    ).fetchone()
    if row is None:
        raise DatabaseError("package-history migration state is missing")
    return int(row[0])


def _table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    return (
        connection.execute(
            "select 1 from sqlite_master where type = 'table' and name = ? limit 1",
            (table_name,),
        ).fetchone()
        is not None
    )


def _table_has_rows(connection: sqlite3.Connection, table_name: str) -> bool:
    table = _SqlIdentifier(table_name)
    return connection.execute(f"select 1 from {table} limit 1").fetchone() is not None


def _row_count(connection: sqlite3.Connection, table_name: str) -> int:
    table = _SqlIdentifier(table_name)
    row = connection.execute(f"select count(*) from {table}").fetchone()
    if row is None:
        raise DatabaseError(f"package-history row count failed for {table_name}")
    return int(row[0])
