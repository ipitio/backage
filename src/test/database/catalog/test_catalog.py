"""Tests for the lazy rotation-independent package catalog."""

import sqlite3
from pathlib import Path

import pytest

from bkg_py.database.composition import DatabaseRepositories
from bkg_py.database.models import (
    OwnerScanPackage,
    PackageCatalogPath,
    PackageInventory,
    PackageRecord,
    PackageRef,
)
from bkg_py.database.owner.queue import OwnerQueueCandidate
from bkg_py.database.settings import DatabaseSettings
from bkg_py.database.support import DatabaseError

from ..repository_support import TODAY, package


def _record(path: PackageCatalogPath, observed_at: str = TODAY) -> PackageRecord:
    package_ref = package(repo=path.repo, package_name=path.package)
    package_ref = PackageRef(
        package_ref.owner_id,
        package_ref.owner_type,
        package_ref.package_type,
        path.owner,
        path.repo,
        path.package,
    )
    return PackageRecord(package_ref, 1, 1, 1, 1, 1, observed_at)


def test_catalog_seed_preserves_tree_paths_across_history_pruning(
    tmp_path: Path,
) -> None:
    """A committed tree seed becomes authoritative and survives rotation."""

    repository = DatabaseRepositories(DatabaseSettings(tmp_path / "index.db"))
    resolved = PackageCatalogPath("Lazztech", "current", "current")
    tree_only = PackageCatalogPath("Historic", "old", "old")
    repository.packages.write_package(_record(resolved, "2026-06-01"))

    status = repository.catalog.initialize_package_catalog(
        (resolved, tree_only),
        "a" * 40,
        TODAY,
    )

    assert status.source_revision == "a" * 40
    assert status.source_inventory == PackageInventory(2, 2, 2)
    assert status.inventory == PackageInventory(2, 2, 2)
    assert status.resolved_packages == 1
    assert repository.packages.package_inventory() == PackageInventory(2, 2, 2)

    repository.packages.cleanup_replaced_legacy_tables(
        since=TODAY,
        prune_normalized=True,
    )

    assert repository.packages.package_inventory() == PackageInventory(2, 2, 2)


@pytest.mark.parametrize("ready", [False, True])
def test_catalog_seed_rolls_back_on_failure(tmp_path: Path, ready: bool) -> None:
    """A failed seed retains the previous catalog and readiness state."""

    path = tmp_path / "index.db"
    repository = DatabaseRepositories(DatabaseSettings(path))
    retained = PackageCatalogPath("Lazztech", "retained", "retained")
    repository.packages.write_package(_record(retained))
    previous = (
        repository.catalog.initialize_package_catalog((retained,), "a" * 40, TODAY)
        if ready
        else None
    )
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            create trigger fail_catalog_seed
            before insert on bkg_package_catalog
            when new.owner = 'Interrupt'
            begin
                select raise(abort, 'interrupted catalog seed');
            end
            """
        )

    with pytest.raises(DatabaseError, match="interrupted catalog seed"):
        repository.catalog.initialize_package_catalog(
            (
                retained,
                PackageCatalogPath("Interrupt", "repo", "package"),
            ),
            "b" * 40,
            TODAY,
        )

    assert repository.catalog.package_catalog_status() == previous
    assert repository.packages.package_inventory() == PackageInventory(1, 1, 1)


def test_catalog_resynchronizes_to_a_new_index_revision(tmp_path: Path) -> None:
    """A newer published tree repairs paths after an incomplete handoff."""

    repository = DatabaseRepositories(DatabaseSettings(tmp_path / "index.db"))
    old = PackageCatalogPath("Alpha", "old", "old")
    retained = PackageCatalogPath("Alpha", "retained", "retained")
    added = PackageCatalogPath("Beta", "added", "added")
    repository.catalog.initialize_package_catalog((old, retained), "a" * 40, TODAY)

    status = repository.catalog.initialize_package_catalog(
        (retained, added),
        "b" * 40,
        "2026-06-11",
    )

    assert status.source_revision == "b" * 40
    assert status.source_inventory == PackageInventory(2, 2, 2)
    assert status.inventory == PackageInventory(2, 2, 2)
    assert repository.packages.package_inventory() == PackageInventory(2, 2, 2)


def test_catalog_resynchronization_preserves_metadata_after_history_pruning(
    tmp_path: Path,
) -> None:
    """A new tree revision cannot turn known retained paths into placeholders."""

    path = tmp_path / "index.db"
    repository = DatabaseRepositories(DatabaseSettings(path))
    retained = PackageCatalogPath("Lazztech", "retained", "retained")
    departed = PackageCatalogPath("Lazztech", "departed", "departed")
    added = PackageCatalogPath("Historic", "new", "new")
    record = _record(retained, "2026-06-01")
    repository.packages.write_package(record)
    repository.catalog.initialize_package_catalog((retained, departed), "a" * 40, TODAY)
    repository.packages.cleanup_replaced_legacy_tables(
        since=TODAY,
        prune_normalized=True,
    )

    status = repository.catalog.initialize_package_catalog(
        (retained, added), "b" * 40, TODAY
    )

    assert status.inventory == status.source_inventory == PackageInventory(2, 2, 2)
    assert status.resolved_packages == 1
    with sqlite3.connect(path) as connection:
        rows = connection.execute(
            """
            select owner, repo, package, owner_id, owner_type, package_type, observed_at
            from bkg_package_catalog order by owner
            """
        ).fetchall()
    assert rows == [
        (added.owner, added.repo, added.package, "", "", "", ""),
        (
            retained.owner,
            retained.repo,
            retained.package,
            record.package_ref.owner_id,
            record.package_ref.owner_type,
            record.package_ref.package_type,
            record.date,
        ),
    ]
    projection = repository.dashboard.dashboard_projection(TODAY)
    assert {item.name: item.packages for item in projection.package_types} == {
        "container": 1,
        "unknown": 1,
    }
    assert {bucket.name: bucket.packages for bucket in projection.freshness} == {
        "today": 0,
        "days_1_7": 0,
        "days_8_30": 1,
        "days_31_plus": 0,
        "unknown": 1,
    }


def test_catalog_resynchronization_keeps_newer_owner_scan_observations(
    tmp_path: Path,
) -> None:
    """Older retained history cannot undo a more recent complete owner scan."""

    path = tmp_path / "index.db"
    repository = DatabaseRepositories(DatabaseSettings(path))
    retained = PackageCatalogPath("Lazztech", "retained", "retained")
    record = _record(retained, "2026-06-01")
    reference = record.package_ref
    repository.packages.write_package(record)
    repository.catalog.initialize_package_catalog((retained,), "a" * 40, TODAY)
    repository.owners.begin_owner_scan(
        reference.owner_id, reference.owner, "scan-1", 100
    )
    repository.owners.observe_owner_scan(
        reference.owner_id,
        "scan-1",
        (
            OwnerScanPackage(
                reference.owner_type,
                reference.package_type,
                retained.repo,
                retained.package,
            ),
        ),
        101,
    )
    repository.owners.complete_owner_scan(reference.owner_id, "scan-1", TODAY, 102)

    repository.catalog.initialize_package_catalog((retained,), "b" * 40, TODAY)

    with sqlite3.connect(path) as connection:
        observed_at = connection.execute(
            "select observed_at from bkg_package_catalog"
        ).fetchone()
    assert observed_at == (TODAY,)


def test_complete_scan_enriches_observed_and_retires_tree_only_paths(
    tmp_path: Path,
) -> None:
    """A complete owner listing reconciles catalog rows without history."""

    repository = DatabaseRepositories(DatabaseSettings(tmp_path / "index.db"))
    retained = PackageCatalogPath("Lazztech", "retained", "retained")
    departed = PackageCatalogPath("Lazztech", "departed", "departed")
    repository.catalog.initialize_package_catalog(
        (retained, departed),
        "c" * 40,
        TODAY,
    )
    package_ref = package(repo=retained.repo, package_name=retained.package)
    assert repository.catalog.unresolved_catalog_owners("batch-1", 10) == ("Lazztech",)
    repository.owners.begin_owner_scan(
        package_ref.owner_id,
        package_ref.owner,
        "scan-1",
        100,
    )
    repository.owners.observe_owner_scan(
        package_ref.owner_id,
        "scan-1",
        (
            OwnerScanPackage(
                package_ref.owner_type,
                package_ref.package_type,
                retained.repo,
                retained.package,
            ),
        ),
        101,
    )

    result = repository.owners.complete_owner_scan(
        package_ref.owner_id,
        "scan-1",
        TODAY,
        102,
    )

    assert result.removed == ()
    assert result.catalog_removed == (departed,)
    assert result.removed_paths == (departed,)
    assert repository.packages.package_inventory() == PackageInventory(1, 1, 1)
    status = repository.catalog.package_catalog_status()
    assert status is not None
    assert status.resolved_packages == 1
    assert repository.catalog.unresolved_catalog_owners("batch-1", 10) == ()


def test_metadata_recovery_is_bounded_distinct_and_advances_past_attempts(
    tmp_path: Path,
) -> None:
    """Partly resolved owners are included without restarting attempted work."""

    repository = DatabaseRepositories(DatabaseSettings(tmp_path / "index.db"))
    assert repository.catalog.unresolved_catalog_owners("batch-1", 2) == ()
    known = PackageCatalogPath("Alpha", "repo", "known")
    repository.packages.write_package(_record(known))
    repository.catalog.initialize_package_catalog(
        (
            known,
            PackageCatalogPath("Alpha", "repo", "unknown"),
            PackageCatalogPath("Beta", "repo", "unknown"),
            PackageCatalogPath("Gamma", "repo", "unknown"),
        ),
        "a" * 40,
        TODAY,
    )
    repository.owner_queue.prepare_owner_queue("batch-1", (), 100)
    assert repository.catalog.unresolved_catalog_owners("batch-1", 0) == ()
    assert repository.catalog.unresolved_catalog_owners("batch-1", 2) == (
        "Alpha",
        "Beta",
    )
    repository.owner_queue.record_owner_queue_candidates(
        "batch-1", (OwnerQueueCandidate("alpha", "catalog-metadata"),), (), 101
    )
    assert repository.catalog.unresolved_catalog_owners("batch-1", 2) == (
        "Beta",
        "Gamma",
    )
    repository.owner_queue.record_owner_queue_candidates(
        "batch-1", (OwnerQueueCandidate("Beta", "stale"),), (), 102
    )
    assert repository.catalog.unresolved_catalog_owners("batch-1", 2) == ("Gamma",)
    repository.owner_queue.prepare_owner_queue("batch-2", (), 103)
    assert repository.catalog.unresolved_catalog_owners("batch-2", 2) == (
        "Alpha",
        "Beta",
    )


def test_catalog_tracks_package_and_owner_retirements(tmp_path: Path) -> None:
    """Application retirement paths update ready catalog totals atomically."""

    repository = DatabaseRepositories(DatabaseSettings(tmp_path / "index.db"))
    first = PackageCatalogPath("Lazztech", "one", "one")
    second = PackageCatalogPath("Lazztech", "two", "two")
    repository.catalog.initialize_package_catalog((first, second), "d" * 40, TODAY)
    first_record = _record(first)
    second_record = _record(second)
    repository.packages.write_package(first_record)
    repository.packages.write_package(second_record)

    repository.packages.retire_package(first_record.package_ref)

    assert repository.packages.package_inventory() == PackageInventory(1, 1, 1)

    repository.packages.retire_owner(second.owner)

    assert repository.packages.package_inventory() == PackageInventory(0, 0, 0)
