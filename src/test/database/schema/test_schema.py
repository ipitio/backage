"""Tests for lazy normalized SQLite schema replacement."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import bkg_py.database.kernel as database_kernel
from bkg_py.database.composition import DatabaseRepositories
from bkg_py.database.models import PackageRecord, PackageRef
from bkg_py.database.settings import DatabaseSettings

_TODAY = "2026-06-10"


def _package(repo: str) -> PackageRef:
    return PackageRef(
        owner_id="69664378",
        owner_type="orgs",
        package_type="container",
        owner="Lazztech",
        repo=repo,
        package="shared",
    )


def test_schema_initialization_runs_once_per_database_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repeated and concurrent operations reuse initialized schema state."""

    path = tmp_path / "index.db"
    repository = DatabaseRepositories(DatabaseSettings(path))
    original = database_kernel.schema.ensure
    calls = 0

    def count_schema_ensure(
        connection: sqlite3.Connection,
        owners: str,
        packages: str,
        versions: str,
    ) -> None:
        nonlocal calls
        calls += 1
        original(connection, owners, packages, versions)

    def ensure_schema(_index: int) -> None:
        repository.kernel.ensure_schema()

    monkeypatch.setattr(database_kernel.schema, "ensure", count_schema_ensure)

    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(ensure_schema, range(8)))

    assert calls == 1

    path.unlink()
    repository.kernel.ensure_schema()

    assert calls == 2


def test_schema_lazily_replaces_old_package_key_without_losing_rows(
    tmp_path: Path,
) -> None:
    """Replacement storage accepts identities blocked by the old primary key."""

    path = tmp_path / "index.db"
    first = _package("FirstRepo")
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            create table packages (
                owner_id text,
                owner_type text not null,
                package_type text not null,
                owner text not null,
                repo text not null,
                package text not null,
                downloads integer not null,
                downloads_month integer not null,
                downloads_week integer not null,
                downloads_day integer not null,
                size integer not null,
                date text not null,
                primary key (owner_id, package, date)
            )
            """
        )
        connection.execute(
            """
            insert into packages values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                first.owner_id,
                first.owner_type,
                first.package_type,
                first.owner,
                first.repo,
                first.package,
                1,
                1,
                1,
                1,
                1,
                _TODAY,
            ),
        )

    repository = DatabaseRepositories(DatabaseSettings(path))
    repository.kernel.ensure_schema()
    repository.kernel.ensure_schema()
    second = _package("SecondRepo")
    repository.packages.write_package(PackageRecord(second, 2, 2, 2, 2, 2, _TODAY))

    with sqlite3.connect(path) as connection:
        legacy_primary_key = tuple(
            str(row[1])
            for row in sorted(
                connection.execute('pragma table_info("packages")'),
                key=lambda row: int(row[5]),
            )
            if int(row[5]) > 0
        )
        rows = connection.execute(
            "select repo, downloads from bkg_package_history order by repo"
        ).fetchall()
        indexes = {
            str(row[0])
            for row in connection.execute(
                "select name from sqlite_master where type = 'index'"
            )
        }
        temporary_table = connection.execute(
            """
            select 1 from sqlite_master
            where type = 'table'
              and name = 'packages__bkg_legacy_primary_key'
            """
        ).fetchone()

    assert legacy_primary_key == ("owner_id", "package", "date")
    assert rows == [("FirstRepo", 1), ("SecondRepo", 2)]
    assert "idx_bkg_history_package_observations_date" in indexes
    assert temporary_table is None

    progress = repository.history.migrate_package_history(10)

    assert progress.migrated_rows == 1
    assert progress.complete
    with sqlite3.connect(path) as connection:
        legacy_exists = connection.execute(
            "select 1 from sqlite_master where type = 'table' and name = 'packages'"
        ).fetchone()
        rows = connection.execute(
            "select repo, downloads from bkg_package_history order by repo"
        ).fetchall()
    assert legacy_exists is None
    assert rows == [("FirstRepo", 1), ("SecondRepo", 2)]
