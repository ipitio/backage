"""Tests for the SQLite kernel, package repository, and legacy replacement."""

import json
import sqlite3
import tempfile
import threading
import time
from pathlib import Path

import pytest

from bkg_py.database.composition import DatabaseRepositories
from bkg_py.database.models import (
    OwnerRecord,
    OwnerScanPackage,
    PackageRecord,
    PackageRef,
    VersionStage,
)
from bkg_py.database.settings import DatabaseSettings
from bkg_py.database.support import DatabaseError, SqlIdentifier, sql
from bkg_py.runtime import GracefulStop

from ..repository_support import (
    TODAY as _TODAY,
)
from ..repository_support import (
    YESTERDAY as _YESTERDAY,
)
from ..repository_support import (
    create_legacy_table as _create_legacy_table,
)
from ..repository_support import (
    insert_legacy as _insert_legacy,
)
from ..repository_support import (
    legacy_table as _legacy_table,
)
from ..repository_support import (
    package as _package,
)
from ..repository_support import (
    version as _version,
)


class TestPackageRepository:
    """Exercise kernel, package storage, fallback, and cleanup behavior."""

    def test_known_owner_type_uses_package_and_active_scan_state(self) -> None:
        """Existing durable owner state avoids a repeated identity request."""

        with tempfile.TemporaryDirectory() as directory:
            repository = DatabaseRepositories(
                DatabaseSettings(Path(directory) / "index.db")
            )
            package = _package()
            assert (
                repository.owners.known_owner_type(package.owner_id, package.owner)
                is None
            )

            repository.packages.write_package(
                PackageRecord(package, 1, 1, 1, 1, 1, _TODAY)
            )
            assert (
                repository.owners.known_owner_type(package.owner_id, package.owner)
                == "orgs"
            )

            repository.owners.begin_owner_scan("7", "ScanOnly", "scan-1", 100)
            repository.owners.observe_owner_scan(
                "7",
                "scan-1",
                (OwnerScanPackage("users", "npm", "repo", "package"),),
                101,
            )
            assert repository.owners.known_owner_type("7", "ScanOnly") == "users"

    def test_package_work_plan_preserves_batch_and_publication_state(self) -> None:
        """One snapshot separates current published rows from pending work."""

        with tempfile.TemporaryDirectory() as directory:
            repository = DatabaseRepositories(
                DatabaseSettings(Path(directory) / "index.db")
            )
            old_package = PackageRef(
                "1", "users", "container", "Alpha", "repo-a", "pkg-a"
            )
            current_package = PackageRef(
                "2", "users", "container", "Beta", "repo-b", "pkg-b"
            )
            unpublished_package = PackageRef(
                "3", "orgs", "container", "Gamma", "repo-c", "pkg-c"
            )
            repository.packages.write_package(
                PackageRecord(old_package, 1, 1, 1, 1, 1, _YESTERDAY)
            )
            repository.packages.write_package(
                PackageRecord(current_package, 1, 1, 1, 1, 1, _TODAY)
            )
            repository.packages.write_package_pending_publication(
                PackageRecord(unpublished_package, 1, 1, 1, 1, 1, _TODAY)
            )
            repository.owners.write_owner(OwnerRecord("4", "Empty", _TODAY))
            repository.owners.write_owner(OwnerRecord("5", "OldEmpty", _YESTERDAY))

            plan = repository.packages.package_work_plan(_TODAY)

            assert len(plan.packages) == 3
            assert plan.packages[0].owner == "Alpha"
            assert tuple(item.owner for item in plan.completed) == ("Beta",)
            assert {item.owner for item in plan.pending} == {"Alpha", "Gamma"}
            assert plan.owners == ("Alpha", "Beta", "Empty", "Gamma")

    def test_package_write_prunes_only_unpaired_partial_version_stages(self) -> None:
        """Successful package work removes superseded interrupted-stage rows."""

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "index.db"
            repository = DatabaseRepositories(DatabaseSettings(path))
            package = _package()
            repository.packages.write_package(
                PackageRecord(package, 1, 1, 1, 1, 1, _YESTERDAY)
            )
            repository.packages.flush_version_stage(
                VersionStage(
                    package,
                    _legacy_table(package),
                    False,
                    (
                        _version("paired-old", date=_YESTERDAY),
                        _version("unpaired-old", date="2026-06-08"),
                        _version("paired-current", date=_TODAY),
                    ),
                )
            )

            repository.packages.write_package_pending_publication(
                PackageRecord(package, 2, 2, 2, 2, 2, _TODAY)
            )

            with sqlite3.connect(path) as connection:
                rows = connection.execute(
                    "select id, date from bkg_version_history order by date, id"
                ).fetchall()
            assert rows == [
                ("paired-old", _YESTERDAY),
                ("paired-current", _TODAY),
            ]

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
            repository = DatabaseRepositories(settings)

            repository.kernel.ensure_schema()
            repository.owners.write_owner(OwnerRecord("1", "owner", _TODAY))
            repository.packages.write_package(
                PackageRecord(_package(), 1, 1, 1, 1, 1, _TODAY)
            )

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
                package_count = connection.execute(
                    "select count(*) from bkg_package_history"
                ).fetchone()[0]

            assert {
                'owner " records',
                "bkg_history_packages",
                "bkg_history_package_observations",
                "bkg_history_versions",
                "bkg_history_version_observations",
            } <= tables
            assert owner_count == 1
            assert package_count == 1

        with tempfile.TemporaryDirectory() as directory:
            repository = DatabaseRepositories(
                DatabaseSettings(
                    Path(directory) / "index.db",
                    owners_table="owners\x00trailing",
                )
            )
            with pytest.raises(DatabaseError, match="cannot contain NUL"):
                repository.kernel.ensure_schema()

    def test_schema_is_lazy_idempotent_and_preserves_existing_tables(self) -> None:
        """Opening an existing database adds only missing normalized structures."""

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "index.db"
            with sqlite3.connect(path) as connection:
                connection.execute("create table retained (value text)")
                connection.execute("insert into retained values ('keep')")
                connection.execute(
                    """
                    create table bkg_owner_scans (
                        owner_id text primary key,
                        owner text not null,
                        marker text not null,
                        status text not null,
                        started_at integer not null,
                        updated_at integer not null,
                        completed_at integer,
                        failure_count integer not null default 0,
                        retry_after integer not null default 0,
                        last_error text not null default ''
                    )
                    """
                )
                connection.execute(
                    """
                    insert into bkg_owner_scans values (
                        '42', 'Example', 'batch:42:100', 'running',
                        100, 101, null, 0, 0, ''
                    )
                    """
                )

            repository = DatabaseRepositories(DatabaseSettings(path))
            repository.kernel.ensure_schema()
            repository.kernel.ensure_schema()

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
                scan_columns = {
                    str(row[1])
                    for row in connection.execute(
                        'pragma table_info("bkg_owner_scans")'
                    )
                }
                scan = connection.execute(
                    "select marker, status, next_page from bkg_owner_scans"
                ).fetchone()

            assert {
                "owners",
                "bkg_history_packages",
                "bkg_history_package_observations",
                "bkg_package_history_state",
                "bkg_history_versions",
                "bkg_history_version_observations",
                "bkg_version_history_state",
                "bkg_owner_scans",
                "bkg_owner_scan_packages",
                "bkg_package_batch_progress",
                "bkg_owner_queue",
            } <= tables
            assert retained == "keep"
            assert "next_page" in scan_columns
            assert scan == ("batch:42:100", "running", 1)
            assert "idx_bkg_history_package_observations_date" in indexes
            assert "idx_bkg_history_version_observations_date" in indexes
            assert "idx_bkg_owner_queue_ready" in indexes

    def test_typed_owner_and_package_writes_match_existing_rows(self) -> None:
        """Typed writes retain the current normalized table representation."""

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "index.db"
            repository = DatabaseRepositories(DatabaseSettings(path))
            package = _package()

            repository.owners.write_owner(
                OwnerRecord(package.owner_id, package.owner, _TODAY)
            )
            repository.packages.write_package(
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
                package_row = connection.execute(
                    "select * from bkg_package_history"
                ).fetchone()

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
            repository = DatabaseRepositories(DatabaseSettings(path))
            package = _package()
            legacy_table = _legacy_table(package)
            repository.kernel.ensure_schema()
            with sqlite3.connect(path) as connection:
                _create_legacy_table(connection, legacy_table)
                _insert_legacy(connection, legacy_table, _version("1"))

            fallback = repository.packages.version_rows(
                package,
                since=_TODAY,
                legacy_table=legacy_table,
            )
            repository.packages.flush_version_stage(
                VersionStage(
                    package_ref=package,
                    legacy_table=legacy_table,
                    write_legacy=False,
                    rows=(_version("2", downloads=200),),
                )
            )
            normalized = repository.packages.version_rows(
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
            repository = DatabaseRepositories(DatabaseSettings(path))
            package = _package()
            legacy_table = _legacy_table(package)
            repository.kernel.ensure_schema()
            with sqlite3.connect(path) as connection:
                _create_legacy_table(connection, legacy_table)

            count = repository.packages.flush_version_stage(
                VersionStage(
                    package_ref=package,
                    legacy_table=legacy_table,
                    write_legacy=True,
                    rows=(_version("1"), _version("2", downloads=200)),
                )
            )

            with sqlite3.connect(path) as connection:
                normalized = connection.execute(
                    "select id, downloads from bkg_version_history order by id"
                ).fetchall()
                legacy = connection.execute(
                    sql(
                        "select id, downloads from {table} order by id",
                        table=SqlIdentifier(legacy_table),
                    )
                ).fetchall()

            assert count == 2
            assert normalized == [("1", 100), ("2", 200)]
            assert legacy == normalized

    def test_version_finalization_commits_after_stop_request(self) -> None:
        """Completed worker rows can be committed before status 3 is returned."""

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "index.db"
            stopped = False

            def check_stop() -> None:
                if stopped:
                    raise GracefulStop("test stop")

            repository = DatabaseRepositories(
                DatabaseSettings(path),
                check_stop=check_stop,
            )
            package = _package()
            stage = VersionStage(
                package_ref=package,
                legacy_table=_legacy_table(package),
                write_legacy=False,
                rows=(_version("1"),),
            )
            repository.kernel.ensure_schema()
            stopped = True

            with pytest.raises(GracefulStop):
                repository.packages.flush_version_stage(stage)

            assert repository.packages.finalize_version_stage(stage) == 1
            with sqlite3.connect(path) as connection:
                count = connection.execute(
                    "select count(*) from bkg_version_history"
                ).fetchone()[0]
            assert count == 1

    def test_failed_legacy_mirror_rolls_back_normalized_batch(self) -> None:
        """A failure after normalized inserts cannot partially commit the batch."""

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "index.db"
            repository = DatabaseRepositories(DatabaseSettings(path))
            package = _package()
            legacy_table = _legacy_table(package)
            repository.kernel.ensure_schema()
            with sqlite3.connect(path) as connection:
                connection.execute(f'create table "{legacy_table}" (id text)')

            with pytest.raises(DatabaseError):
                repository.packages.flush_version_stage(
                    VersionStage(
                        package_ref=package,
                        legacy_table=legacy_table,
                        write_legacy=True,
                        rows=(_version("1"),),
                    )
                )

            with sqlite3.connect(path) as connection:
                count = connection.execute(
                    "select count(*) from bkg_version_history"
                ).fetchone()[0]
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
            repository = DatabaseRepositories(settings)
            repository.kernel.ensure_schema()
            lock = sqlite3.connect(path, isolation_level=None)
            lock.execute("begin immediate")
            errors: list[DatabaseError] = []

            def write() -> None:
                try:
                    repository.owners.write_owner(
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
            repository = DatabaseRepositories(DatabaseSettings(path))
            repository.kernel.ensure_schema()
            with sqlite3.connect(path) as connection:
                connection.execute(f'create table "{legacy_table}" (id text)')

            with pytest.raises(DatabaseError):
                repository.packages.flush_version_stage(VersionStage.load(stage_dir))

            assert manifest_path.is_file()
            assert row_path.is_file()

    def test_legacy_cleanup_waits_for_replacement_and_rotation_drops_orphans(
        self,
    ) -> None:
        """Current fallback rows survive until replaced; orphan tables do not."""

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "index.db"
            repository = DatabaseRepositories(DatabaseSettings(path))
            package = _package()
            legacy_table = _legacy_table(package)
            orphan_table = "versions_orgs_container_Lazztech_Orphan_orphan"
            repository.packages.write_package(
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

            dropped = repository.packages.cleanup_legacy_package(
                package,
                legacy_table,
                since=_TODAY,
            )
            with sqlite3.connect(path) as connection:
                remaining = connection.execute(
                    sql(
                        "select id from {table}",
                        table=SqlIdentifier(legacy_table),
                    )
                ).fetchall()
            assert not dropped
            assert remaining == [("1",)]

            repository.packages.flush_version_stage(
                VersionStage(
                    package_ref=package,
                    legacy_table=legacy_table,
                    write_legacy=False,
                    rows=(_version("1"),),
                )
            )
            assert repository.packages.cleanup_legacy_package(
                package, legacy_table, since=_TODAY
            )
            assert repository.packages.cleanup_replaced_legacy_tables(since=_TODAY) == 1
            with sqlite3.connect(path) as connection:
                tables = {
                    row[0]
                    for row in connection.execute(
                        "select name from sqlite_master where type = 'table'"
                    )
                }
            assert legacy_table not in tables
            assert orphan_table not in tables

    def test_rotation_prune_removes_old_normalized_rows_and_legacy_tables(
        self,
    ) -> None:
        """Rotation keeps current rows and removes replaced legacy fallback tables."""

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "index.db"
            repository = DatabaseRepositories(DatabaseSettings(path))
            package = _package()
            legacy_table = _legacy_table(package)
            orphan_table = "versions_orgs_container_Lazztech_Orphan_orphan"
            repository.packages.write_package(
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
            repository.packages.write_package(
                PackageRecord(
                    package_ref=package,
                    downloads=2,
                    downloads_month=2,
                    downloads_week=2,
                    downloads_day=2,
                    size=2,
                    date=_TODAY,
                )
            )
            repository.packages.flush_version_stage(
                VersionStage(
                    package_ref=package,
                    legacy_table=legacy_table,
                    write_legacy=False,
                    rows=(
                        _version("1", date=_YESTERDAY),
                        _version("2", date=_TODAY),
                    ),
                )
            )
            with sqlite3.connect(path) as connection:
                _create_legacy_table(connection, legacy_table)
                _create_legacy_table(connection, orphan_table)
                _insert_legacy(
                    connection, legacy_table, _version("old", date=_YESTERDAY)
                )
                _insert_legacy(connection, legacy_table, _version("2"))
                _insert_legacy(connection, orphan_table, _version("9"))

            assert (
                repository.packages.cleanup_replaced_legacy_tables(
                    since=_TODAY,
                    prune_normalized=True,
                    vacuum=True,
                )
                == 2
            )

            with sqlite3.connect(path) as connection:
                package_dates = connection.execute(
                    "select date from bkg_package_history order by date"
                ).fetchall()
                version_dates = connection.execute(
                    "select date from bkg_version_history order by date"
                ).fetchall()
                tables = {
                    row[0]
                    for row in connection.execute(
                        "select name from sqlite_master where type = 'table'"
                    )
                }
            assert package_dates == [(_TODAY,)]
            assert version_dates == [(_TODAY,)]
            assert legacy_table not in tables
            assert orphan_table not in tables

    def test_retire_owner_removes_normalized_rows_and_known_legacy_tables(
        self,
    ) -> None:
        """Unavailable owners leave no database rows or package legacy tables."""

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "index.db"
            repository = DatabaseRepositories(DatabaseSettings(path))
            package = _package()
            legacy_table = _legacy_table(package)
            repository.owners.write_owner(
                OwnerRecord(package.owner_id, package.owner, _TODAY)
            )
            repository.packages.write_package(
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
            repository.packages.flush_version_stage(
                VersionStage(
                    package_ref=package,
                    legacy_table=legacy_table,
                    write_legacy=False,
                    rows=(_version("1"),),
                )
            )
            with sqlite3.connect(path) as connection:
                _create_legacy_table(connection, legacy_table)

            assert repository.packages.retire_owner(package.owner) == 3
            with sqlite3.connect(path) as connection:
                for table in (
                    "owners",
                    "bkg_package_history",
                    "bkg_version_history",
                ):
                    assert (
                        connection.execute(
                            sql(
                                "select count(*) from {table} where owner = ?",
                                table=SqlIdentifier(table),
                            ),
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
