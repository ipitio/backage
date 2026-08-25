"""Tests for lazy version-history replacement and compatibility reads."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from bkg_py.database.composition import DatabaseRepositories
from bkg_py.database.models import VersionStage
from bkg_py.database.settings import DatabaseSettings
from bkg_py.database.values import normalized_version_values

from ..repository_support import (
    TODAY,
    YESTERDAY,
    create_normalized_version_table,
    package,
    version,
)


def _legacy_database(path: Path) -> None:
    package_ref = package()
    records = (
        version("1", date=YESTERDAY, downloads=10),
        version("1", date=TODAY, downloads=20),
        version("2", date=TODAY, downloads=30),
    )
    with sqlite3.connect(path) as connection:
        create_normalized_version_table(connection)
        connection.executemany(
            """
            insert into versions values (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (normalized_version_values(package_ref, record) for record in records),
        )


def test_partial_migration_resumes_and_prefers_replacement_writes(
    tmp_path: Path,
) -> None:
    """Committed batches remain readable and newer replacement rows win."""

    path = tmp_path / "index.db"
    _legacy_database(path)
    repository = DatabaseRepositories(DatabaseSettings(path))
    repository.kernel.ensure_schema()
    repository.packages.flush_version_stage(
        VersionStage(
            package(),
            "unused",
            False,
            (version("1", date=TODAY, downloads=999),),
        )
    )

    first = repository.history.migrate_version_history(1)
    assert first.migrated_rows == 1
    assert first.remaining_rows == 2
    assert not first.complete
    with sqlite3.connect(path) as connection:
        partial_state = connection.execute(
            """
            select phase, migrated_rows, remaining_rows
            from bkg_version_history_state
            where singleton = 1
            """
        ).fetchone()
    assert partial_state == ("migrating", 1, 2)
    rows_during_migration = repository.packages.version_rows(
        package(), since=YESTERDAY
    ).rows
    assert len(rows_during_migration) == 3
    assert (
        next(
            row
            for row in rows_during_migration
            if row.version_id == "1" and row.date == TODAY
        ).metrics.downloads
        == 999
    )

    completed = DatabaseRepositories(
        DatabaseSettings(path)
    ).history.migrate_version_history(10)
    assert completed.migrated_rows == 2
    assert completed.complete
    with sqlite3.connect(path) as connection:
        tables = {
            str(row[0])
            for row in connection.execute(
                "select name from sqlite_master where type = 'table'"
            )
        }
        state = connection.execute(
            """
            select phase, migrated_rows, remaining_rows
            from bkg_version_history_state
            where singleton = 1
            """
        ).fetchone()
        observations = connection.execute(
            "select count(*) from bkg_history_version_observations"
        ).fetchone()[0]
    assert "versions" not in tables
    assert state == ("ready", 3, 0)
    assert observations == 3


def test_rotation_cleanup_finishes_retained_rows_and_drops_legacy_table(
    tmp_path: Path,
) -> None:
    """A rotation cannot retain the table that version history replaces."""

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
            "select 1 from sqlite_master where type = 'table' and name = 'versions'"
        ).fetchone()
        dates = connection.execute(
            "select date from bkg_version_history order by id"
        ).fetchall()
    assert legacy_exists is None
    assert dates == [(TODAY,), (TODAY,)]
