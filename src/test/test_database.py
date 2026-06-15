"""Tests for the SQLite repository and lazy legacy replacement."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import time
from pathlib import Path

import pytest

from bkg_py.database import (
    DatabaseError,
    DatabaseRepository,
    DatabaseSettings,
    OwnerRecord,
    OwnerScanFailure,
    OwnerScanPackage,
    PackageRecord,
    PackageRef,
    VersionRecord,
    VersionStage,
)
from bkg_py.database_models import VersionMetrics

_TODAY = "2026-06-10"
_YESTERDAY = "2026-06-09"


def _package(repo: str = "Libre-Closet", package: str = "libre-closet") -> PackageRef:
    return PackageRef(
        owner_id="69664378",
        owner_type="orgs",
        package_type="container",
        owner="Lazztech",
        repo=repo,
        package=package,
    )


def _version(
    version_id: str,
    *,
    date: str = _TODAY,
    downloads: int = 100,
) -> VersionRecord:
    return VersionRecord(
        version_id=version_id,
        name=f"sha256:{version_id}",
        metrics=VersionMetrics(
            size=123,
            downloads=downloads,
            downloads_month=10,
            downloads_week=5,
            downloads_day=1,
        ),
        date=date,
        tags="latest" if version_id == "2" else "",
    )


def _legacy_table(package: PackageRef) -> str:
    return (
        f"versions_{package.owner_type}_{package.package_type}_{package.owner}_"
        f"{package.repo}_{package.package}"
    )


def _create_legacy_table(connection: sqlite3.Connection, table: str) -> None:
    quoted = table.replace('"', '""')
    columns = ", ".join(
        (
            "id text not null",
            "name text not null",
            "size integer not null",
            "downloads integer not null",
            "downloads_month integer not null",
            "downloads_week integer not null",
            "downloads_day integer not null",
            "date text not null",
            "tags text",
            "primary key (id, date)",
        )
    )
    connection.execute(f'create table "{quoted}" ({columns})')


def _insert_legacy(
    connection: sqlite3.Connection,
    table: str,
    version: VersionRecord,
) -> None:
    quoted = table.replace('"', '""')
    metrics = version.metrics
    connection.execute(
        f"""
        insert into "{quoted}" (
            id, name, size, downloads, downloads_month, downloads_week,
            downloads_day, date, tags
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            version.version_id,
            version.name,
            metrics.size,
            metrics.downloads,
            metrics.downloads_month,
            metrics.downloads_week,
            metrics.downloads_day,
            version.date,
            version.tags,
        ),
    )


class TestDatabaseRepository:
    """Exercise schema, retry, transaction, fallback, and cleanup behavior."""

    def test_table_identifiers_are_quoted_and_nul_is_rejected(self) -> None:
        """Configured names remain identifiers even when they resemble SQL."""

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "index.db"
            settings = DatabaseSettings(
                path,
                owners_table='owner " records',
                packages_table="select",
                versions_table="versions; drop table select",
            )
            repository = DatabaseRepository(settings)

            repository.ensure_schema()
            repository.write_owner(OwnerRecord("1", "owner", _TODAY))

            with sqlite3.connect(path) as connection:
                tables = {
                    str(row[0])
                    for row in connection.execute(
                        "select name from sqlite_master where type = 'table'"
                    )
                }
                owner_count = connection.execute(
                    'select count(*) from "owner "" records"'
                ).fetchone()[0]

            assert {
                'owner " records',
                "select",
                "versions; drop table select",
            } <= tables
            assert owner_count == 1

        with tempfile.TemporaryDirectory() as directory:
            repository = DatabaseRepository(
                DatabaseSettings(
                    Path(directory) / "index.db",
                    owners_table="owners\x00trailing",
                )
            )
            with pytest.raises(DatabaseError, match="cannot contain NUL"):
                repository.ensure_schema()

    def test_schema_is_lazy_idempotent_and_preserves_existing_tables(self) -> None:
        """Opening an existing database adds only missing normalized structures."""

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "index.db"
            with sqlite3.connect(path) as connection:
                connection.execute("create table retained (value text)")
                connection.execute("insert into retained values ('keep')")

            repository = DatabaseRepository(DatabaseSettings(path))
            repository.ensure_schema()
            repository.ensure_schema()

            with sqlite3.connect(path) as connection:
                tables = {
                    row[0]
                    for row in connection.execute(
                        "select name from sqlite_master where type = 'table'"
                    )
                }
                indexes = {
                    row[0]
                    for row in connection.execute(
                        "select name from sqlite_master where type = 'index'"
                    )
                }
                retained = connection.execute("select value from retained").fetchone()[
                    0
                ]

            assert {
                "owners",
                "packages",
                "versions",
                "bkg_owner_scans",
                "bkg_owner_scan_packages",
            } <= tables
            assert retained == "keep"
            assert "idx_bkg_packages_owner_repo_package_date" in indexes
            assert "idx_bkg_versions_package_date" in indexes

    def test_typed_owner_and_package_writes_match_existing_rows(self) -> None:
        """Typed writes retain the current normalized table representation."""

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "index.db"
            repository = DatabaseRepository(DatabaseSettings(path))
            package = _package()

            repository.write_owner(OwnerRecord(package.owner_id, package.owner, _TODAY))
            repository.write_package(
                PackageRecord(
                    package_ref=package,
                    downloads=2000,
                    downloads_month=300,
                    downloads_week=200,
                    downloads_day=20,
                    size=400,
                    date=_TODAY,
                )
            )

            with sqlite3.connect(path) as connection:
                owner_row = connection.execute("select * from owners").fetchone()
                package_row = connection.execute("select * from packages").fetchone()

            assert owner_row == ("69664378", "Lazztech", _TODAY)
            assert package_row == (
                "69664378",
                "orgs",
                "container",
                "Lazztech",
                "Libre-Closet",
                "libre-closet",
                2000,
                300,
                200,
                20,
                400,
                _TODAY,
            )

    def test_version_reads_prefer_normalized_rows_then_fall_back(self) -> None:
        """One normalized row makes normalized storage authoritative."""

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "index.db"
            repository = DatabaseRepository(DatabaseSettings(path))
            package = _package()
            legacy_table = _legacy_table(package)
            repository.ensure_schema()
            with sqlite3.connect(path) as connection:
                _create_legacy_table(connection, legacy_table)
                _insert_legacy(connection, legacy_table, _version("1"))

            fallback = repository.version_rows(
                package,
                since=_TODAY,
                legacy_table=legacy_table,
            )
            repository.flush_version_stage(
                VersionStage(
                    package_ref=package,
                    legacy_table=legacy_table,
                    write_legacy=False,
                    rows=(_version("2", downloads=200),),
                )
            )
            normalized = repository.version_rows(
                package,
                since=_TODAY,
                legacy_table=legacy_table,
            )

            assert fallback.source == "legacy"
            assert [row.version_id for row in fallback.rows] == ["1"]
            assert normalized.source == "normalized"
            assert [row.version_id for row in normalized.rows] == ["2"]

    def test_version_batch_mirrors_legacy_rows_in_one_transaction(self) -> None:
        """A successful batch commits matching normalized and legacy rows."""

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "index.db"
            repository = DatabaseRepository(DatabaseSettings(path))
            package = _package()
            legacy_table = _legacy_table(package)
            repository.ensure_schema()
            with sqlite3.connect(path) as connection:
                _create_legacy_table(connection, legacy_table)

            count = repository.flush_version_stage(
                VersionStage(
                    package_ref=package,
                    legacy_table=legacy_table,
                    write_legacy=True,
                    rows=(_version("1"), _version("2", downloads=200)),
                )
            )

            with sqlite3.connect(path) as connection:
                normalized = connection.execute(
                    "select id, downloads from versions order by id"
                ).fetchall()
                legacy = connection.execute(
                    f'select id, downloads from "{legacy_table}" order by id'
                ).fetchall()

            assert count == 2
            assert normalized == [("1", 100), ("2", 200)]
            assert legacy == normalized

    def test_failed_legacy_mirror_rolls_back_normalized_batch(self) -> None:
        """A failure after normalized inserts cannot partially commit the batch."""

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "index.db"
            repository = DatabaseRepository(DatabaseSettings(path))
            package = _package()
            legacy_table = _legacy_table(package)
            repository.ensure_schema()
            with sqlite3.connect(path) as connection:
                connection.execute(f'create table "{legacy_table}" (id text)')

            with pytest.raises(DatabaseError):
                repository.flush_version_stage(
                    VersionStage(
                        package_ref=package,
                        legacy_table=legacy_table,
                        write_legacy=True,
                        rows=(_version("1"),),
                    )
                )

            with sqlite3.connect(path) as connection:
                count = connection.execute("select count(*) from versions").fetchone()[
                    0
                ]
            assert count == 0

    def test_locked_write_retries_until_database_is_available(self) -> None:
        """Transient real SQLite locking is retried with the configured policy."""

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "index.db"
            settings = DatabaseSettings(
                path,
                busy_timeout_ms=5,
                max_attempts=20,
                retry_delay_seconds=0.01,
            )
            repository = DatabaseRepository(settings)
            repository.ensure_schema()
            lock = sqlite3.connect(path, isolation_level=None)
            lock.execute("begin immediate")
            errors: list[DatabaseError] = []

            def write() -> None:
                try:
                    repository.write_owner(
                        OwnerRecord("1", "locked-owner", _TODAY),
                    )
                except DatabaseError as error:  # pragma: no cover - assertion aid
                    errors.append(error)

            worker = threading.Thread(target=write)
            worker.start()
            time.sleep(0.08)
            lock.rollback()
            lock.close()
            worker.join(timeout=5)

            assert not worker.is_alive()
            assert not errors
            with sqlite3.connect(path) as connection:
                count = connection.execute("select count(*) from owners").fetchone()[0]
            assert count == 1

    def test_failed_stage_flush_leaves_stage_files_resumable(self) -> None:
        """Loading or committing a stage never consumes its source records."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "index.db"
            stage_dir = root / "stage"
            stage_dir.mkdir()
            package = _package()
            legacy_table = _legacy_table(package)
            manifest = {
                "owner_id": package.owner_id,
                "owner_type": package.owner_type,
                "package_type": package.package_type,
                "owner": package.owner,
                "repo": package.repo,
                "package": package.package,
                "legacy_table": legacy_table,
                "write_legacy": True,
            }
            row = {
                "id": "1",
                "name": "sha256:1",
                "size": 123,
                "downloads": 100,
                "downloads_month": 10,
                "downloads_week": 5,
                "downloads_day": 1,
                "date": _TODAY,
                "tags": "",
            }
            manifest_path = stage_dir / "manifest.json"
            row_path = stage_dir / "row.000001.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            row_path.write_text(json.dumps(row), encoding="utf-8")
            repository = DatabaseRepository(DatabaseSettings(path))
            repository.ensure_schema()
            with sqlite3.connect(path) as connection:
                connection.execute(f'create table "{legacy_table}" (id text)')

            with pytest.raises(DatabaseError):
                repository.flush_version_stage(VersionStage.load(stage_dir))

            assert manifest_path.is_file()
            assert row_path.is_file()

    def test_legacy_cleanup_waits_for_replacement_and_rotation_drops_orphans(
        self,
    ) -> None:
        """Current fallback rows survive until replaced; orphan tables do not."""

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "index.db"
            repository = DatabaseRepository(DatabaseSettings(path))
            package = _package()
            legacy_table = _legacy_table(package)
            orphan_table = "versions_orgs_container_Lazztech_Orphan_orphan"
            repository.write_package(
                PackageRecord(
                    package_ref=package,
                    downloads=1,
                    downloads_month=1,
                    downloads_week=1,
                    downloads_day=1,
                    size=1,
                    date=_TODAY,
                )
            )
            with sqlite3.connect(path) as connection:
                _create_legacy_table(connection, legacy_table)
                _create_legacy_table(connection, orphan_table)
                _insert_legacy(
                    connection, legacy_table, _version("old", date=_YESTERDAY)
                )
                _insert_legacy(connection, legacy_table, _version("1"))
                _insert_legacy(connection, orphan_table, _version("9"))

            dropped = repository.cleanup_legacy_package(
                package,
                legacy_table,
                since=_TODAY,
            )
            with sqlite3.connect(path) as connection:
                remaining = connection.execute(
                    f'select id from "{legacy_table}"'
                ).fetchall()
            assert not dropped
            assert remaining == [("1",)]

            repository.flush_version_stage(
                VersionStage(
                    package_ref=package,
                    legacy_table=legacy_table,
                    write_legacy=False,
                    rows=(_version("1"),),
                )
            )
            assert repository.cleanup_legacy_package(
                package, legacy_table, since=_TODAY
            )
            assert repository.cleanup_replaced_legacy_tables(since=_TODAY) == 1
            with sqlite3.connect(path) as connection:
                tables = {
                    row[0]
                    for row in connection.execute(
                        "select name from sqlite_master where type = 'table'"
                    )
                }
            assert legacy_table not in tables
            assert orphan_table not in tables

    def test_retire_owner_removes_normalized_rows_and_known_legacy_tables(
        self,
    ) -> None:
        """Unavailable owners leave no database rows or package legacy tables."""

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "index.db"
            repository = DatabaseRepository(DatabaseSettings(path))
            package = _package()
            legacy_table = _legacy_table(package)
            repository.write_owner(OwnerRecord(package.owner_id, package.owner, _TODAY))
            repository.write_package(
                PackageRecord(
                    package_ref=package,
                    downloads=1,
                    downloads_month=1,
                    downloads_week=1,
                    downloads_day=1,
                    size=1,
                    date=_TODAY,
                )
            )
            repository.flush_version_stage(
                VersionStage(
                    package_ref=package,
                    legacy_table=legacy_table,
                    write_legacy=False,
                    rows=(_version("1"),),
                )
            )
            with sqlite3.connect(path) as connection:
                _create_legacy_table(connection, legacy_table)

            assert repository.retire_owner(package.owner) == 3
            with sqlite3.connect(path) as connection:
                for table in ("owners", "packages", "versions"):
                    assert (
                        connection.execute(
                            f"select count(*) from {table} where owner = ?",
                            (package.owner,),
                        ).fetchone()[0]
                        == 0
                    )
                tables = {
                    str(row[0])
                    for row in connection.execute(
                        "select name from sqlite_master where type = 'table'"
                    )
                }
            assert legacy_table not in tables

    def test_completed_owner_scan_reconciles_only_unobserved_packages(
        self,
    ) -> None:
        """A verified complete scan removes absent package data atomically."""

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "index.db"
            repository = DatabaseRepository(DatabaseSettings(path))
            retained = _package(repo="retained", package="retained")
            removed = _package(repo="removed", package="removed")
            for package in (retained, removed):
                repository.write_package(
                    PackageRecord(
                        package_ref=package,
                        downloads=1,
                        downloads_month=1,
                        downloads_week=1,
                        downloads_day=1,
                        size=1,
                        date=_TODAY,
                    )
                )
                repository.flush_version_stage(
                    VersionStage(
                        package_ref=package,
                        legacy_table=_legacy_table(package),
                        write_legacy=False,
                        rows=(_version("1"),),
                    )
                )
            with sqlite3.connect(path) as connection:
                _create_legacy_table(connection, _legacy_table(removed))

            repository.begin_owner_scan(
                retained.owner_id,
                retained.owner,
                "scan-1",
                100,
            )
            assert repository.owner_scan_active(retained.owner_id, "scan-1")
            assert not repository.owner_scan_active(retained.owner_id, "scan-old")
            repository.observe_owner_scan(
                retained.owner_id,
                "scan-1",
                (
                    OwnerScanPackage(
                        retained.owner_type,
                        retained.package_type,
                        retained.repo,
                        retained.package,
                    ),
                ),
                101,
            )

            assert repository.missing_owner_scan_packages(
                retained.owner_id,
                "scan-1",
            ) == (removed,)
            result = repository.complete_owner_scan(
                retained.owner_id,
                "scan-1",
                _TODAY,
                102,
            )

            assert result.removed == (removed,)
            assert result.pending_count == 0
            assert result.retry_after == 0
            assert not repository.owner_scan_active(retained.owner_id, "scan-1")
            with sqlite3.connect(path) as connection:
                packages = connection.execute(
                    "select repo, package from packages order by repo"
                ).fetchall()
                versions = connection.execute(
                    "select repo, package from versions order by repo"
                ).fetchall()
                scan = connection.execute(
                    """
                    select status, failure_count, retry_after
                    from bkg_owner_scans
                    where owner_id = ?
                    """,
                    (retained.owner_id,),
                ).fetchone()
                staged = connection.execute(
                    "select count(*) from bkg_owner_scan_packages"
                ).fetchone()[0]
                legacy_exists = connection.execute(
                    """
                    select count(*) from sqlite_master
                    where type = 'table' and name = ?
                    """,
                    (_legacy_table(removed),),
                ).fetchone()[0]

            assert packages == [("retained", "retained")]
            assert versions == [("retained", "retained")]
            assert scan == ("completed", 0, 0)
            assert staged == 0
            assert legacy_exists == 0

    def test_incomplete_owner_refresh_uses_persisted_exponential_backoff(
        self,
    ) -> None:
        """Incomplete refreshes defer automatic selection until retry time."""

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "index.db"
            repository = DatabaseRepository(
                DatabaseSettings(
                    path,
                    owner_retry_initial_seconds=10,
                    owner_retry_max_seconds=40,
                )
            )
            package = _package()
            repository.write_package(
                PackageRecord(
                    package_ref=package,
                    downloads=1,
                    downloads_month=1,
                    downloads_week=1,
                    downloads_day=1,
                    size=1,
                    date=_YESTERDAY,
                )
            )
            repository.begin_owner_scan(
                package.owner_id,
                package.owner,
                "scan-1",
                100,
            )
            repository.observe_owner_scan(
                package.owner_id,
                "scan-1",
                (
                    OwnerScanPackage(
                        package.owner_type,
                        package.package_type,
                        package.repo,
                        package.package,
                    ),
                ),
                101,
            )

            result = repository.complete_owner_scan(
                package.owner_id,
                "scan-1",
                _TODAY,
                102,
            )
            assert result.pending_count == 1
            assert result.retry_after == 112
            assert repository.deferred_owners(111) == ((package.owner, 112),)
            assert repository.deferred_owners(112) == ()

            retry_after = repository.fail_owner_scan(
                OwnerScanFailure(
                    package.owner_id,
                    package.owner,
                    None,
                    "still unavailable",
                    113,
                )
            )
            assert retry_after == 133
            assert repository.deferred_owners(120) == ((package.owner, 133),)

            repository.clear_owner_backoff(
                package.owner_id,
                package.owner,
                121,
            )
            assert repository.deferred_owners(120) == ()
