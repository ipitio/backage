"""Tests for durable owner scan cursors and package work selection."""

from __future__ import annotations

from pathlib import Path

import pytest

from bkg_py.database import DatabaseError, DatabaseRepository, DatabaseSettings
from bkg_py.database_models import (
    OwnerScanPackage,
    OwnerScanPage,
    OwnerScanStart,
    OwnerScanWorkSelection,
    PackageRecord,
    PackageRef,
)

_TODAY = "2026-06-10"


def _package(repo: str, package: str) -> PackageRef:
    return PackageRef(
        owner_id="69664378",
        owner_type="orgs",
        package_type="container",
        owner="Lazztech",
        repo=repo,
        package=package,
    )


def _record(package: PackageRef, date: str = _TODAY) -> PackageRecord:
    return PackageRecord(
        package_ref=package,
        downloads=1,
        downloads_month=1,
        downloads_week=1,
        downloads_day=1,
        size=1,
        date=date,
    )


def test_owner_scan_cursor_replays_pages_and_selects_pending_work(
    tmp_path: Path,
) -> None:
    """A page advances only after stale and unpublished work is selected."""

    repository = DatabaseRepository(DatabaseSettings(tmp_path / "index.db"))
    current = _package("current", "current")
    unpublished = _package("pending", "pending")
    discovered = _package("new", "new")
    repository.write_package(_record(current))
    repository.write_package_pending_publication(_record(unpublished))
    observed = tuple(
        OwnerScanPackage(
            package.owner_type,
            package.package_type,
            package.repo,
            package.package,
        )
        for package in (current, unpublished, discovered)
    )
    cursor = repository.begin_or_resume_owner_scan(
        OwnerScanStart(current.owner_id, current.owner, "batch-1", 100)
    )

    assert cursor.next_page == 1
    assert not cursor.resumed
    with pytest.raises(DatabaseError, match="expected page 1, got 2"):
        repository.observe_owner_scan_page(
            OwnerScanPage(current.owner_id, cursor.marker, 2, 101),
            observed,
        )
    repository.observe_owner_scan_page(
        OwnerScanPage(current.owner_id, cursor.marker, 1, 101),
        observed,
    )
    assert (
        repository.owner_scan_packages_needing_refresh(
            OwnerScanWorkSelection(
                current.owner_id,
                current.owner,
                observed,
                _TODAY,
            )
        )
        == observed[1:]
    )

    page = OwnerScanPage(current.owner_id, cursor.marker, 1, 102)
    repository.advance_owner_scan_page(page)
    repository.advance_owner_scan_page(page)
    resumed = repository.begin_or_resume_owner_scan(
        OwnerScanStart(current.owner_id, current.owner, "batch-1", 103)
    )
    assert resumed.next_page == 2
    assert resumed.resumed


def test_owner_scan_staging_keeps_one_repository_identity_per_package(
    tmp_path: Path,
) -> None:
    """Later observations replace repository aliases for one natural package."""

    repository = DatabaseRepository(DatabaseSettings(tmp_path / "index.db"))
    old = _package("old-repo", "shared")
    current = _package("current-repo", "shared")
    repository.write_package(_record(old, "2026-06-09"))
    repository.write_package(_record(current))
    repository.begin_owner_scan(old.owner_id, old.owner, "scan-1", 100)

    repository.observe_owner_scan(
        old.owner_id,
        "scan-1",
        (
            OwnerScanPackage(
                old.owner_type,
                old.package_type,
                old.repo,
                old.package,
            ),
            OwnerScanPackage(
                current.owner_type,
                current.package_type,
                current.repo,
                current.package,
            ),
        ),
        101,
    )

    assert repository.missing_owner_scan_packages(old.owner_id, "scan-1") == (old,)


def test_owner_refresh_plan_uses_normalized_rows_and_publication_state(
    tmp_path: Path,
) -> None:
    """Direct owner work is selected from current data and pending files."""

    repository = DatabaseRepository(DatabaseSettings(tmp_path / "index.db"))
    stale = _package("stale-repo", "stale")
    unpublished = _package("pending-repo", "pending")
    current = _package("current-repo", "current")
    repository.write_package(_record(stale, "2026-06-09"))

    stale_only = repository.owner_refresh_plan(
        stale.owner_id,
        stale.owner,
        _TODAY,
    )
    assert not stale_only.partially_updated
    assert stale_only.packages == (
        OwnerScanPackage("orgs", "container", "stale-repo", "stale"),
    )

    repository.write_package_pending_publication(_record(unpublished))
    repository.write_package(_record(current))
    plan = repository.owner_refresh_plan(current.owner_id, current.owner, _TODAY)

    assert plan.partially_updated
    assert plan.packages == (
        OwnerScanPackage("orgs", "container", "stale-repo", "stale"),
        OwnerScanPackage("orgs", "container", "pending-repo", "pending"),
    )

    repository.write_package(_record(stale))
    repository.clear_package_publication(unpublished)
    complete = repository.owner_refresh_plan(current.owner_id, current.owner, _TODAY)
    assert complete.partially_updated
    assert complete.pending_count == 0
