"""Tests for lazy package-observation replacement and compatibility reads."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from bkg_py.database.composition import DatabaseRepositories
from bkg_py.database.models import PackageRecord
from bkg_py.database.settings import DatabaseSettings
from bkg_py.database.values import package_values

from .repository_support import (
    TODAY,
    YESTERDAY,
    create_normalized_package_table,
    package,
)


def _record(date: str, downloads: int) -> PackageRecord:
    return PackageRecord(package(), downloads, 10, 5, 1, 123, date)


def _legacy_database(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        create_normalized_package_table(connection)
        connection.executemany(
            "insert into packages values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                (*package_values(package()), 10, 10, 5, 1, 123, YESTERDAY),
                (*package_values(package()), 20, 10, 5, 1, 123, TODAY),
            ),
        )


def test_partial_migration_resumes_and_prefers_replacement_writes(
    tmp_path: Path,
) -> None:
    """Committed rows remain readable while replacement observations win."""

    path = tmp_path / "index.db"
    _legacy_database(path)
    repository = DatabaseRepositories(DatabaseSettings(path))
    repository.kernel.ensure_schema()
    repository.packages.write_package(_record(TODAY, 999))

    first = repository.history.migrate_package_history(1)

    assert first.migrated_rows == 1
    assert first.remaining_rows == 1
    assert not first.complete
    with sqlite3.connect(path) as connection:
        state = connection.execute(
            """
            select phase, migrated_rows, remaining_rows
            from bkg_package_history_state where singleton = 1
            """
        ).fetchone()
        rows = connection.execute(
            "select date, downloads from bkg_package_history order by date"
        ).fetchall()
    assert state == ("migrating", 1, 1)
    assert rows == [(YESTERDAY, 10), (TODAY, 999)]

    completed = DatabaseRepositories(
        DatabaseSettings(path)
    ).history.migrate_package_history(10)

    assert completed.migrated_rows == 1
    assert completed.complete
    with sqlite3.connect(path) as connection:
        legacy_exists = connection.execute(
            "select 1 from sqlite_master where type = 'table' and name = 'packages'"
        ).fetchone()
        state = connection.execute(
            """
            select phase, migrated_rows, remaining_rows
            from bkg_package_history_state where singleton = 1
            """
        ).fetchone()
        observations = connection.execute(
            "select count(*) from bkg_history_package_observations"
        ).fetchone()[0]
    assert legacy_exists is None
    assert state == ("ready", 2, 0)
    assert observations == 2


def test_rotation_cleanup_finishes_retained_rows_and_drops_legacy_table(
    tmp_path: Path,
) -> None:
    """Rotation retains current package history only in replacement storage."""

    path = tmp_path / "index.db"
    _legacy_database(path)
    repository = DatabaseRepositories(DatabaseSettings(path))
    repository.kernel.ensure_schema()

    repository.packages.cleanup_replaced_legacy_tables(
        since=TODAY,
        prune_normalized=True,
    )

    with sqlite3.connect(path) as connection:
        legacy_exists = connection.execute(
            "select 1 from sqlite_master where type = 'table' and name = 'packages'"
        ).fetchone()
        rows = connection.execute(
            "select date, downloads from bkg_package_history"
        ).fetchall()
    assert legacy_exists is None
    assert rows == [(TODAY, 20)]


def test_version_pruning_preserves_package_only_identities(tmp_path: Path) -> None:
    """Version cleanup cannot delete an identity used by package history."""

    path = tmp_path / "index.db"
    repository = DatabaseRepositories(DatabaseSettings(path))
    repository.packages.write_package(_record(TODAY, 20))

    repository.packages.cleanup_replaced_legacy_tables(
        since=TODAY,
        prune_normalized=True,
    )

    with sqlite3.connect(path) as connection:
        rows = connection.execute(
            "select owner_id, date from bkg_package_history"
        ).fetchall()
    assert rows == [(package().owner_id, TODAY)]
