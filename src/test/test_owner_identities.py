"""Tests for lazy reconciliation of superseded GitHub owner IDs."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from bkg_py.database import DatabaseRepository, DatabaseSettings
from bkg_py.database_models import OwnerRecord, PackageRecord, PackageRef

_TODAY = "2026-07-01"
_YESTERDAY = "2026-06-30"


def _package(
    owner_id: str,
    repo: str,
    package: str,
    owner: str = "Alpha",
) -> PackageRef:
    return PackageRef(
        owner_id,
        "users",
        "container",
        owner,
        repo,
        package,
    )


def _legacy_table(package: PackageRef) -> str:
    return (
        f"versions_{package.owner_type}_{package.package_type}_{package.owner}_"
        f"{package.repo}_{package.package}"
    )


def _create_legacy_table(connection: sqlite3.Connection, table: str) -> None:
    quoted = table.replace('"', '""')
    connection.execute(f'create table "{quoted}" (id text primary key)')


def _owner_ids(connection: sqlite3.Connection, table: str) -> list[tuple[str]]:
    statements = {
        "packages": "select distinct owner_id from packages order by owner_id",
        "owners": "select distinct owner_id from owners order by owner_id",
        "bkg_owner_scans": (
            "select distinct owner_id from bkg_owner_scans order by owner_id"
        ),
        "bkg_package_publications": (
            "select distinct owner_id from bkg_package_publications order by owner_id"
        ),
    }
    return connection.execute(statements[table]).fetchall()


def _owners(connection: sqlite3.Connection, table: str) -> list[tuple[str]]:
    statements = {
        "packages": "select distinct owner from packages order by owner",
        "owners": "select distinct owner from owners order by owner",
        "bkg_owner_scans": "select distinct owner from bkg_owner_scans order by owner",
        "bkg_package_publications": (
            "select distinct owner from bkg_package_publications order by owner"
        ),
    }
    return connection.execute(statements[table]).fetchall()


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "select name from sqlite_master where type = 'table'"
        )
    }


def test_retire_owner_aliases_preserves_current_package_paths(
    tmp_path: Path,
) -> None:
    """Verified current IDs replace stale identities without losing shared data."""

    database_path = tmp_path / "index.db"
    repository = DatabaseRepository(DatabaseSettings(database_path))
    current = _package("200", "shared", "same")
    old_shared = _package("100", "shared", "same")
    old_orphan = _package("100", "old", "removed")
    repository.write_package(PackageRecord(current, 2, 2, 2, 2, 2, _TODAY))
    repository.write_package(PackageRecord(old_shared, 1, 1, 1, 1, 1, _YESTERDAY))
    repository.write_package_pending_publication(
        PackageRecord(old_orphan, 1, 1, 1, 1, 1, _YESTERDAY)
    )
    repository.write_owner(OwnerRecord("100", "Alpha", _YESTERDAY))
    repository.write_owner(OwnerRecord("200", "Alpha", _TODAY))
    repository.begin_owner_scan("100", "Alpha", "old-scan", 1)
    shared_legacy = _legacy_table(old_shared)
    orphan_legacy = _legacy_table(old_orphan)
    with sqlite3.connect(database_path) as connection:
        _create_legacy_table(connection, shared_legacy)
        _create_legacy_table(connection, orphan_legacy)

    assert repository.owner_alias_ids("200", "alpha") == ("100",)

    cleanup = repository.retire_owner_aliases("200", "Alpha")

    assert cleanup.alias_ids == ("100",)
    assert cleanup.orphaned_packages == (old_orphan,)
    assert not repository.owner_alias_ids("200", "Alpha")
    with sqlite3.connect(database_path) as connection:
        package_ids = _owner_ids(connection, "packages")
        owner_ids = _owner_ids(connection, "owners")
        scan_ids = _owner_ids(connection, "bkg_owner_scans")
        marker_ids = _owner_ids(connection, "bkg_package_publications")
        tables = _table_names(connection)
    assert package_ids == [("200",)]
    assert owner_ids == [("200",)]
    assert not scan_ids
    assert not marker_ids
    assert shared_legacy in tables
    assert orphan_legacy not in tables


def test_retire_owner_aliases_removes_legacy_null_owner_ids(
    tmp_path: Path,
) -> None:
    """Malformed nullable package identities cannot hold a batch open forever."""

    database_path = tmp_path / "index.db"
    repository = DatabaseRepository(DatabaseSettings(database_path))
    current = _package("200", "shared", "same")
    repository.write_package(PackageRecord(current, 2, 2, 2, 2, 2, _TODAY))
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            insert into packages (
                owner_id, owner_type, package_type, owner, repo, package,
                downloads, downloads_month, downloads_week, downloads_day,
                size, date
            ) values (null, 'users', 'container', 'Alpha', 'shared', 'same',
                      1, 1, 1, 1, 1, ?)
            """,
            (_YESTERDAY,),
        )

    assert len(repository.package_work_plan(_TODAY).pending) == 1
    assert repository.owner_alias_ids("200", "alpha") == ("",)

    cleanup = repository.retire_owner_aliases("200", "Alpha")

    assert cleanup.alias_ids == ("",)
    assert not cleanup.orphaned_packages
    assert not repository.package_work_plan(_TODAY).pending


def test_retire_owner_aliases_removes_same_id_owner_name_aliases(
    tmp_path: Path,
) -> None:
    """Verified logins replace stale casing and rename rows for one owner ID."""

    database_path = tmp_path / "index.db"
    repository = DatabaseRepository(DatabaseSettings(database_path))
    current = _package("200", "shared", "same")
    stale_case = _package("200", "old-case", "removed-case", "alpha")
    stale_name = _package("200", "old-name", "removed-name", "OldAlpha")
    repository.write_package(PackageRecord(current, 2, 2, 2, 2, 2, _TODAY))
    repository.write_package_pending_publication(
        PackageRecord(stale_case, 1, 1, 1, 1, 1, _YESTERDAY)
    )
    repository.write_package(PackageRecord(stale_name, 1, 1, 1, 1, 1, _YESTERDAY))
    repository.mark_package_batch_completed(stale_name, "batch-old", _YESTERDAY)
    repository.write_owner(OwnerRecord("200", "alpha", _YESTERDAY))
    repository.write_owner(OwnerRecord("200", "Alpha", _TODAY))
    repository.begin_owner_scan("200", "alpha", "old-scan", 1)
    legacy_tables = (_legacy_table(stale_case), _legacy_table(stale_name))
    with sqlite3.connect(database_path) as connection:
        for table in legacy_tables:
            _create_legacy_table(connection, table)

    assert not repository.owner_alias_ids("200", "Alpha")
    assert repository.owner_has_aliases("200", "Alpha")

    cleanup = repository.retire_owner_aliases("200", "Alpha")

    assert cleanup.alias_ids == ()
    assert cleanup.orphaned_packages == (stale_case, stale_name)
    assert not repository.owner_has_aliases("200", "Alpha")
    with sqlite3.connect(database_path) as connection:
        owners_by_table = {
            table: _owners(connection, table)
            for table in (
                "packages",
                "owners",
                "bkg_owner_scans",
                "bkg_package_publications",
            )
        }
        progress_count = connection.execute(
            "select count(*) from bkg_package_batch_progress"
        ).fetchone()[0]
        tables = _table_names(connection)
    assert owners_by_table["packages"] == [("Alpha",)]
    assert owners_by_table["owners"] == [("Alpha",)]
    assert not owners_by_table["bkg_owner_scans"]
    assert not owners_by_table["bkg_package_publications"]
    assert progress_count == 0
    assert not set(legacy_tables) & tables
