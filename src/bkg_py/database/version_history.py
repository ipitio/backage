"""Lazy normalized version-history storage and compatibility reads."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .models import PackageRef, VersionStage
from .support import DatabaseError
from .values import package_values

PACKAGE_IDENTITIES_TABLE = "bkg_history_packages"
VERSION_IDENTITIES_TABLE = "bkg_history_versions"
VERSION_OBSERVATIONS_TABLE = "bkg_history_version_observations"
VERSION_HISTORY_STATE_TABLE = "bkg_version_history_state"
VERSION_HISTORY_VIEW = "bkg_version_history"
MIGRATION_BATCH_ROWS = 50_000

DATA_SCHEMA_SQL = (
    f"""
    create table if not exists "{PACKAGE_IDENTITIES_TABLE}" (
        package_key integer primary key,
        owner_id text not null,
        owner_type text not null,
        package_type text not null,
        owner text not null,
        repo text not null,
        package text not null,
        unique (owner_id, owner_type, package_type, owner, repo, package)
    )
    """,
    f"""
    create table if not exists "{VERSION_IDENTITIES_TABLE}" (
        version_key integer primary key,
        package_key integer not null references "{PACKAGE_IDENTITIES_TABLE}"
            on delete cascade,
        external_id text not null,
        name text not null,
        unique (package_key, external_id, name)
    )
    """,
    f"""
    create table if not exists "{VERSION_OBSERVATIONS_TABLE}" (
        version_key integer not null references "{VERSION_IDENTITIES_TABLE}"
            on delete cascade,
        date text not null,
        size integer not null,
        downloads integer not null,
        downloads_month integer not null,
        downloads_week integer not null,
        downloads_day integer not null,
        tags text,
        primary key (version_key, date)
    ) without rowid
    """,
    f"""
    create index if not exists "idx_bkg_history_version_observations_date"
    on "{VERSION_OBSERVATIONS_TABLE}" (date)
    """,
)
_STATE_SCHEMA_SQL = f"""
    create table if not exists "{VERSION_HISTORY_STATE_TABLE}" (
        singleton integer primary key check (singleton = 1),
        phase text not null check (phase in ('migrating', 'ready')),
        migrated_rows integer not null default 0,
        remaining_rows integer not null default 0,
        updated_at text not null
    )
"""
_STATE_UPSERT_SQL = f"""
    insert into "{VERSION_HISTORY_STATE_TABLE}" (
        singleton, phase, migrated_rows, remaining_rows, updated_at
    ) values (1, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
    on conflict(singleton) do update set
        phase = excluded.phase,
        migrated_rows = "{VERSION_HISTORY_STATE_TABLE}".migrated_rows
            + excluded.migrated_rows,
        remaining_rows = excluded.remaining_rows,
        updated_at = excluded.updated_at
"""
_PACKAGE_IDENTITY_COLUMNS = (
    "owner_id",
    "owner_type",
    "package_type",
    "owner",
    "repo",
    "package",
)
_RESERVED_NAMES = frozenset(
    {
        PACKAGE_IDENTITIES_TABLE,
        VERSION_IDENTITIES_TABLE,
        VERSION_OBSERVATIONS_TABLE,
        VERSION_HISTORY_STATE_TABLE,
        VERSION_HISTORY_VIEW,
    }
)


class _SqlIdentifier(str):
    """A SQLite identifier quoted before statement construction."""

    def __new__(cls, value: str) -> _SqlIdentifier:
        if "\x00" in value:
            raise DatabaseError("SQLite identifiers cannot contain NUL")
        return str.__new__(cls, f'"{value.replace(chr(34), chr(34) * 2)}"')


@dataclass(frozen=True)
class VersionHistoryMigration:
    """Progress from one committed delete-after-copy migration batch."""

    migrated_rows: int
    remaining_rows: int

    @property
    def complete(self) -> bool:
        """Return whether the compatibility table has been removed."""

        return self.remaining_rows == 0


def ensure(connection: sqlite3.Connection, legacy_table_name: str) -> None:
    """Create the replacement layout and the appropriate compatibility view."""

    if legacy_table_name in _RESERVED_NAMES:
        raise DatabaseError(
            f"version table name is reserved by history storage: {legacy_table_name}"
        )
    for statement in (*DATA_SCHEMA_SQL, _STATE_SCHEMA_SQL):
        connection.execute(statement)
    _ensure_state_columns(connection)

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


def write_stage(connection: sqlite3.Connection, stage: VersionStage) -> None:
    """Upsert one complete stage into identity and observation storage."""

    if not stage.rows:
        return
    package_key = _package_key(connection, stage.package_ref)
    connection.executemany(
        f"""
        insert into "{VERSION_IDENTITIES_TABLE}" (
            package_key, external_id, name
        ) values (?, ?, ?)
        on conflict(package_key, external_id, name) do nothing
        """,
        ((package_key, row.version_id, row.name) for row in stage.rows),
    )
    version_keys = {
        (str(row[1]), str(row[2])): int(row[0])
        for row in connection.execute(
            f"""
            select version_key, external_id, name
            from "{VERSION_IDENTITIES_TABLE}"
            where package_key = ?
            """,
            (package_key,),
        )
    }
    observations: list[tuple[int | str | None, ...]] = []
    superseded: list[tuple[int | str, ...]] = []
    for row in stage.rows:
        version_key = version_keys[(row.version_id, row.name)]
        metrics = row.metrics
        superseded.append((package_key, row.version_id, row.date, version_key))
        observations.append(
            (
                version_key,
                row.date,
                metrics.size,
                metrics.downloads,
                metrics.downloads_month,
                metrics.downloads_week,
                metrics.downloads_day,
                row.tags,
            )
        )
    connection.executemany(
        f"""
        delete from "{VERSION_OBSERVATIONS_TABLE}"
        where version_key in (
            select version_key from "{VERSION_IDENTITIES_TABLE}"
            where package_key = ? and external_id = ?
        )
          and date = ?
          and version_key != ?
        """,
        superseded,
    )
    connection.executemany(
        f"""
        insert into "{VERSION_OBSERVATIONS_TABLE}" (
            version_key, date, size, downloads, downloads_month,
            downloads_week, downloads_day, tags
        ) values (?, ?, ?, ?, ?, ?, ?, ?)
        on conflict(version_key, date) do update set
            size = excluded.size,
            downloads = excluded.downloads,
            downloads_month = excluded.downloads_month,
            downloads_week = excluded.downloads_week,
            downloads_day = excluded.downloads_day,
            tags = excluded.tags
        """,
        observations,
    )
    _prune_package_key(connection, package_key)


def migrate_batch(
    connection: sqlite3.Connection,
    legacy_table_name: str,
    row_limit: int,
) -> VersionHistoryMigration:
    """Move one bounded legacy row batch and delete it in the same transaction."""

    if row_limit <= 0:
        raise ValueError("version-history migration row limit must be positive")
    if not _table_exists(connection, legacy_table_name):
        _mark_state(connection, "ready", remaining_rows=0)
        _replace_view(connection, None)
        return VersionHistoryMigration(0, 0)

    legacy = _SqlIdentifier(legacy_table_name)
    connection.execute(
        """
        create temp table if not exists "bkg_history_migration_rows" (
            legacy_rowid integer primary key
        ) without rowid
        """
    )
    connection.execute('delete from "bkg_history_migration_rows"')
    connection.execute(
        f"""
        insert into "bkg_history_migration_rows" (legacy_rowid)
        select rowid from {legacy} order by rowid limit ?
        """,
        (row_limit,),
    )
    migrated_rows = _row_count(connection, "bkg_history_migration_rows")
    if migrated_rows == 0:
        _finish_migration(connection, legacy_table_name)
        return VersionHistoryMigration(0, 0)

    _copy_selected_rows(connection, legacy)
    connection.execute(
        f"""
        delete from {legacy}
        where rowid in (
            select legacy_rowid from "bkg_history_migration_rows"
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
        return VersionHistoryMigration(migrated_rows, 0)
    remaining_rows = max(0, _state_remaining_rows(connection) - migrated_rows)
    _mark_state(
        connection,
        "migrating",
        migrated_rows,
        remaining_rows=remaining_rows,
    )
    return VersionHistoryMigration(migrated_rows, remaining_rows)


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


def delete_unpaired(
    connection: sqlite3.Connection,
    packages_table_name: str,
    package: PackageRef,
) -> None:
    """Remove staged observations that no package observation can publish."""

    packages = _SqlIdentifier(packages_table_name)
    identity = package_values(package)
    connection.execute(
        f"""
        delete from "{VERSION_OBSERVATIONS_TABLE}"
        where version_key in (
            select versions.version_key
            from "{VERSION_IDENTITIES_TABLE}" versions
            join "{PACKAGE_IDENTITIES_TABLE}" identities
              on identities.package_key = versions.package_key
            where identities.owner_id = ? and identities.owner_type = ?
              and identities.package_type = ? and identities.owner = ?
              and identities.repo = ? and identities.package = ?
        )
          and not exists (
              select 1 from {packages}
              where owner_id = ? and owner_type = ? and package_type = ?
                and owner = ? and repo = ? and package = ?
                and {packages}.date = "{VERSION_OBSERVATIONS_TABLE}".date
          )
        """,
        (*identity, *identity),
    )
    package_key = _existing_package_key(connection, package)
    if package_key is not None:
        _prune_package_key(connection, package_key)


def retire_package(connection: sqlite3.Connection, package: PackageRef) -> None:
    """Delete replacement history for one package identity."""

    connection.execute(
        f"""
        delete from "{PACKAGE_IDENTITIES_TABLE}"
        where owner_id = ? and owner_type = ? and package_type = ?
          and owner = ? and repo = ? and package = ?
        """,
        package_values(package),
    )


def retire_owner(connection: sqlite3.Connection, owner: str) -> int:
    """Delete replacement history for one unavailable owner."""

    row = connection.execute(
        f"""
        select count(*)
        from "{VERSION_OBSERVATIONS_TABLE}" observations
        join "{VERSION_IDENTITIES_TABLE}" versions
          on versions.version_key = observations.version_key
        join "{PACKAGE_IDENTITIES_TABLE}" packages
          on packages.package_key = versions.package_key
        where packages.owner = ?
        """,
        (owner,),
    ).fetchone()
    deleted = 0 if row is None else int(row[0])
    connection.execute(
        f'delete from "{PACKAGE_IDENTITIES_TABLE}" where owner = ?',
        (owner,),
    )
    return deleted


def retire_owner_aliases(
    connection: sqlite3.Connection,
    owner_id: str,
    owner: str,
) -> None:
    """Delete replacement history belonging to superseded owner identities."""

    connection.execute(
        f"""
        delete from "{PACKAGE_IDENTITIES_TABLE}"
        where (owner_id = ? and owner != ? collate binary)
           or (owner = ? collate nocase and owner_id != ?)
        """,
        (owner_id, owner, owner, owner_id),
    )


def prune_before(connection: sqlite3.Connection, since: str) -> None:
    """Delete replacement observations older than the retained history window."""

    connection.execute(
        f'delete from "{VERSION_OBSERVATIONS_TABLE}" where date < ?',
        (since,),
    )
    _prune_identities(connection)


def _package_key(connection: sqlite3.Connection, package: PackageRef) -> int:
    identity = package_values(package)
    connection.execute(
        f"""
        insert into "{PACKAGE_IDENTITIES_TABLE}" (
            {", ".join(_PACKAGE_IDENTITY_COLUMNS)}
        ) values (?, ?, ?, ?, ?, ?)
        on conflict({", ".join(_PACKAGE_IDENTITY_COLUMNS)}) do nothing
        """,
        identity,
    )
    package_key = _existing_package_key(connection, package)
    if package_key is None:
        raise DatabaseError("version-history package identity was not persisted")
    return package_key


def _existing_package_key(
    connection: sqlite3.Connection,
    package: PackageRef,
) -> int | None:
    row = connection.execute(
        f"""
        select package_key from "{PACKAGE_IDENTITIES_TABLE}"
        where owner_id = ? and owner_type = ? and package_type = ?
          and owner = ? and repo = ? and package = ?
        """,
        package_values(package),
    ).fetchone()
    return None if row is None else int(row[0])


def _copy_selected_rows(
    connection: sqlite3.Connection,
    legacy: _SqlIdentifier,
) -> None:
    selected_join = (
        'join "bkg_history_migration_rows" selected '
        "on selected.legacy_rowid = legacy.rowid"
    )
    identity_join = f"""
        join "{PACKAGE_IDENTITIES_TABLE}" packages
          on packages.owner_id = legacy.owner_id
         and packages.owner_type = legacy.owner_type
         and packages.package_type = legacy.package_type
         and packages.owner = legacy.owner
         and packages.repo = legacy.repo
         and packages.package = legacy.package
    """
    connection.execute(
        f"""
        insert into "{PACKAGE_IDENTITIES_TABLE}" (
            {", ".join(_PACKAGE_IDENTITY_COLUMNS)}
        )
        select {", ".join(f"legacy.{column}" for column in _PACKAGE_IDENTITY_COLUMNS)}
        from {legacy} legacy
        {selected_join}
        group by {", ".join(f"legacy.{column}" for column in _PACKAGE_IDENTITY_COLUMNS)}
        on conflict({", ".join(_PACKAGE_IDENTITY_COLUMNS)}) do nothing
        """
    )
    connection.execute(
        f"""
        insert into "{VERSION_IDENTITIES_TABLE}" (
            package_key, external_id, name
        )
        select packages.package_key, legacy.id, legacy.name
        from {legacy} legacy
        {selected_join}
        {identity_join}
        group by packages.package_key, legacy.id, legacy.name
        on conflict(package_key, external_id, name) do nothing
        """
    )
    connection.execute(
        f"""
        insert into "{VERSION_OBSERVATIONS_TABLE}" (
            version_key, date, size, downloads, downloads_month,
            downloads_week, downloads_day, tags
        )
        select versions.version_key, legacy.date, legacy.size,
               legacy.downloads, legacy.downloads_month,
               legacy.downloads_week, legacy.downloads_day, legacy.tags
        from {legacy} legacy
        {selected_join}
        {identity_join}
        join "{VERSION_IDENTITIES_TABLE}" versions
          on versions.package_key = packages.package_key
         and versions.external_id = legacy.id
         and versions.name = legacy.name
        where not exists (
            select 1
            from "{VERSION_IDENTITIES_TABLE}" current_versions
            join "{VERSION_OBSERVATIONS_TABLE}" current_observations
              on current_observations.version_key = current_versions.version_key
            where current_versions.package_key = packages.package_key
              and current_versions.external_id = legacy.id
              and current_observations.date = legacy.date
        )
        on conflict(version_key, date) do nothing
        """
    )


def _replace_view(
    connection: sqlite3.Connection,
    legacy_table_name: str | None,
) -> None:
    connection.execute(f'drop view if exists "{VERSION_HISTORY_VIEW}"')
    candidate = _candidate_view_select()
    if legacy_table_name is None:
        connection.execute(f'create view "{VERSION_HISTORY_VIEW}" as {candidate}')
        return
    legacy = _SqlIdentifier(legacy_table_name)
    connection.execute(
        f"""
        create view "{VERSION_HISTORY_VIEW}" as
        select legacy.owner_id, legacy.owner_type, legacy.package_type,
               legacy.owner, legacy.repo, legacy.package, legacy.id,
               legacy.name, legacy.size, legacy.downloads,
               legacy.downloads_month, legacy.downloads_week,
               legacy.downloads_day, legacy.date, legacy.tags
        from {legacy} legacy
        where not exists (
            select 1
            from "{PACKAGE_IDENTITIES_TABLE}" packages
            join "{VERSION_IDENTITIES_TABLE}" versions
              on versions.package_key = packages.package_key
            join "{VERSION_OBSERVATIONS_TABLE}" observations
              on observations.version_key = versions.version_key
            where packages.owner_id = legacy.owner_id
              and packages.owner_type = legacy.owner_type
              and packages.package_type = legacy.package_type
              and packages.owner = legacy.owner
              and packages.repo = legacy.repo
              and packages.package = legacy.package
              and versions.external_id = legacy.id
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
               versions.external_id as id, versions.name,
               observations.size, observations.downloads,
               observations.downloads_month, observations.downloads_week,
               observations.downloads_day, observations.date,
               observations.tags
        from "{PACKAGE_IDENTITIES_TABLE}" packages
        join "{VERSION_IDENTITIES_TABLE}" versions
          on versions.package_key = packages.package_key
        join "{VERSION_OBSERVATIONS_TABLE}" observations
          on observations.version_key = versions.version_key
    """


def _finish_migration(
    connection: sqlite3.Connection,
    legacy_table_name: str,
) -> None:
    connection.execute(f'drop view if exists "{VERSION_HISTORY_VIEW}"')
    if _table_exists(connection, legacy_table_name):
        legacy = _SqlIdentifier(legacy_table_name)
        connection.execute(f"drop table {legacy}")
    _prune_identities(connection)
    _mark_state(connection, "ready", remaining_rows=0)
    _replace_view(connection, None)


def _prune_package_key(connection: sqlite3.Connection, package_key: int) -> None:
    connection.execute(
        f"""
        delete from "{VERSION_IDENTITIES_TABLE}"
        where package_key = ?
          and not exists (
              select 1 from "{VERSION_OBSERVATIONS_TABLE}" observations
              where observations.version_key =
                    "{VERSION_IDENTITIES_TABLE}".version_key
          )
        """,
        (package_key,),
    )
    connection.execute(
        f"""
        delete from "{PACKAGE_IDENTITIES_TABLE}"
        where package_key = ?
          and not exists (
              select 1 from "{VERSION_IDENTITIES_TABLE}" versions
              where versions.package_key =
                    "{PACKAGE_IDENTITIES_TABLE}".package_key
          )
        """,
        (package_key,),
    )


def _prune_identities(connection: sqlite3.Connection) -> None:
    connection.execute(
        f"""
        delete from "{VERSION_IDENTITIES_TABLE}"
        where not exists (
            select 1 from "{VERSION_OBSERVATIONS_TABLE}" observations
            where observations.version_key =
                  "{VERSION_IDENTITIES_TABLE}".version_key
        )
        """
    )
    connection.execute(
        f"""
        delete from "{PACKAGE_IDENTITIES_TABLE}"
        where not exists (
            select 1 from "{VERSION_IDENTITIES_TABLE}" versions
            where versions.package_key = "{PACKAGE_IDENTITIES_TABLE}".package_key
        )
        """
    )


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
        select remaining_rows from "{VERSION_HISTORY_STATE_TABLE}"
        where singleton = 1
        """
    ).fetchone()
    if row is None:
        raise DatabaseError("version-history migration state is missing")
    return int(row[0])


def _ensure_state_columns(connection: sqlite3.Connection) -> None:
    columns = {
        str(row[1])
        for row in connection.execute(
            f'pragma table_info("{VERSION_HISTORY_STATE_TABLE}")'
        )
    }
    if "remaining_rows" not in columns:
        connection.execute(
            f"""
            alter table "{VERSION_HISTORY_STATE_TABLE}"
            add column remaining_rows integer not null default 0
            """
        )


def _table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    return (
        connection.execute(
            """
            select 1 from sqlite_master
            where type = 'table' and name = ?
            limit 1
            """,
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
        raise DatabaseError(f"version-history row count failed for {table_name}")
    return int(row[0])
