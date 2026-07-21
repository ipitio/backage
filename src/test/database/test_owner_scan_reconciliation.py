"""Tests for durable owner scan reconciliation and retry state."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from bkg_py.database import (
    DatabaseRepository,
    DatabaseSettings,
    OwnerScanFailure,
    OwnerScanPackage,
    PackageRecord,
    VersionStage,
)

from .repository_support import (
    TODAY,
    YESTERDAY,
    create_legacy_table,
    legacy_table,
    package,
    version,
)


def test_completed_owner_scan_reconciles_only_unobserved_packages(
    tmp_path: Path,
) -> None:
    """A verified complete scan removes absent package data atomically."""

    database_path = tmp_path / "index.db"
    repository = DatabaseRepository(DatabaseSettings(database_path))
    retained = package(repo="retained", package_name="retained")
    removed = package(repo="removed", package_name="removed")
    for package_ref in (retained, removed):
        repository.write_package(
            PackageRecord(
                package_ref=package_ref,
                downloads=1,
                downloads_month=1,
                downloads_week=1,
                downloads_day=1,
                size=1,
                date=TODAY,
            )
        )
        repository.flush_version_stage(
            VersionStage(
                package_ref=package_ref,
                legacy_table=legacy_table(package_ref),
                write_legacy=False,
                rows=(version("1"),),
            )
        )
    with sqlite3.connect(database_path) as connection:
        create_legacy_table(connection, legacy_table(removed))

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
        TODAY,
        102,
    )

    assert result.removed == (removed,)
    assert result.pending_count == 0
    assert result.retry_after == 0
    assert not repository.owner_scan_active(retained.owner_id, "scan-1")
    with sqlite3.connect(database_path) as connection:
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
            (legacy_table(removed),),
        ).fetchone()[0]

    assert packages == [("retained", "retained")]
    assert versions == [("retained", "retained")]
    assert scan == ("completed", 0, 0)
    assert staged == 0
    assert legacy_exists == 0


def test_incomplete_owner_refresh_uses_persisted_exponential_backoff(
    tmp_path: Path,
) -> None:
    """Incomplete refreshes defer automatic selection until retry time."""

    repository = DatabaseRepository(
        DatabaseSettings(
            tmp_path / "index.db",
            owner_retry_initial_seconds=10,
            owner_retry_max_seconds=40,
        )
    )
    package_ref = package()
    repository.write_package(
        PackageRecord(
            package_ref=package_ref,
            downloads=1,
            downloads_month=1,
            downloads_week=1,
            downloads_day=1,
            size=1,
            date=YESTERDAY,
        )
    )
    repository.begin_owner_scan(
        package_ref.owner_id,
        package_ref.owner,
        "scan-1",
        100,
    )
    repository.observe_owner_scan(
        package_ref.owner_id,
        "scan-1",
        (
            OwnerScanPackage(
                package_ref.owner_type,
                package_ref.package_type,
                package_ref.repo,
                package_ref.package,
            ),
        ),
        101,
    )

    result = repository.complete_owner_scan(
        package_ref.owner_id,
        "scan-1",
        TODAY,
        102,
    )
    assert result.pending_count == 1
    assert result.retry_after == 112
    assert repository.deferred_owners(111) == ((package_ref.owner, 112),)
    assert repository.deferred_owners(112) == ()

    retry_after = repository.fail_owner_scan(
        OwnerScanFailure(
            package_ref.owner_id,
            package_ref.owner,
            None,
            "still unavailable",
            113,
        )
    )
    assert retry_after == 133
    assert repository.deferred_owners(120) == ((package_ref.owner, 133),)

    repository.clear_owner_backoff(
        package_ref.owner_id,
        package_ref.owner,
        121,
    )
    assert repository.deferred_owners(120) == ()


def test_pending_publication_keeps_current_owner_package_incomplete(
    tmp_path: Path,
) -> None:
    """Owner reconciliation waits for generated package files to publish."""

    repository = DatabaseRepository(
        DatabaseSettings(
            tmp_path / "index.db",
            owner_retry_initial_seconds=10,
            owner_retry_max_seconds=40,
        )
    )
    package_ref = package()
    repository.write_package_pending_publication(
        PackageRecord(
            package_ref=package_ref,
            downloads=1,
            downloads_month=1,
            downloads_week=1,
            downloads_day=1,
            size=1,
            date=TODAY,
        )
    )
    repository.begin_owner_scan(
        package_ref.owner_id,
        package_ref.owner,
        "scan-1",
        100,
    )
    repository.observe_owner_scan(
        package_ref.owner_id,
        "scan-1",
        (
            OwnerScanPackage(
                package_ref.owner_type,
                package_ref.package_type,
                package_ref.repo,
                package_ref.package,
            ),
        ),
        101,
    )

    result = repository.complete_owner_scan(
        package_ref.owner_id,
        "scan-1",
        TODAY,
        102,
    )

    assert result.pending_count == 1
    assert result.retry_after == 112
