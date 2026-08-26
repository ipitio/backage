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


def test_catalog_seed_rolls_back_before_readiness_on_failure(tmp_path: Path) -> None:
    """An interrupted first seed leaves history inventory authoritative."""

    path = tmp_path / "index.db"
    repository = DatabaseRepositories(DatabaseSettings(path))
    retained = PackageCatalogPath("Lazztech", "retained", "retained")
    repository.packages.write_package(_record(retained))
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

    assert repository.catalog.package_catalog_status() is None
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
