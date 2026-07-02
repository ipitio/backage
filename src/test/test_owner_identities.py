"""Tests for lazy reconciliation of superseded GitHub owner IDs."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from bkg_py.database import DatabaseRepository, DatabaseSettings
from bkg_py.database_models import OwnerRecord, PackageRecord, PackageRef

_TODAY = "2026-07-01"
_YESTERDAY = "2026-06-30"


def _package(owner_id: str, repo: str, package: str) -> PackageRef:
    return PackageRef(
        owner_id,
        "users",
        "container",
        "Alpha",
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

    cleanup = repository.retire_owner_aliases("200", "alpha")

    assert cleanup.alias_ids == ("100",)
    assert cleanup.orphaned_packages == (old_orphan,)
    assert not repository.owner_alias_ids("200", "Alpha")
    with sqlite3.connect(database_path) as connection:
        package_ids = _owner_ids(connection, "packages")
        owner_ids = _owner_ids(connection, "owners")
        scan_ids = _owner_ids(connection, "bkg_owner_scans")
        marker_ids = _owner_ids(connection, "bkg_package_publications")
        tables = {
            str(row[0])
            for row in connection.execute(
                "select name from sqlite_master where type = 'table'"
            )
        }
    assert package_ids == [("200",)]
    assert owner_ids == [("200",)]
    assert not scan_ids
    assert not marker_ids
    assert shared_legacy in tables
    assert orphan_legacy not in tables
