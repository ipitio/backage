"""Tests for durable per-package batch generation progress."""

from dataclasses import replace
from pathlib import Path

import pytest

from bkg_py.database.composition import DatabaseRepositories
from bkg_py.database.models import (
    PackageCatalogPath,
    PackageInventory,
    PackageRecord,
    PackageRef,
)
from bkg_py.database.settings import DatabaseSettings

_TODAY = "2026-06-10"


def test_package_work_plan_distinguishes_same_day_batch_generations(
    tmp_path: Path,
) -> None:
    """A new marker makes a same-day package due without changing its date."""

    repository = DatabaseRepositories(DatabaseSettings(tmp_path / "index.db"))
    package = PackageRef(
        "69664378",
        "orgs",
        "container",
        "Lazztech",
        "Libre-Closet",
        "libre-closet",
    )
    repository.packages.write_package(PackageRecord(package, 1, 1, 1, 1, 1, _TODAY))

    first = repository.packages.package_work_plan(_TODAY, "batch-1")
    assert len(first.pending) == 1

    repository.packages.bootstrap_package_batch("batch-1", _TODAY)
    bootstrapped = repository.packages.package_work_plan(_TODAY, "batch-1")
    assert len(bootstrapped.completed) == 1

    next_batch = repository.packages.package_work_plan(_TODAY, "batch-2")
    assert len(next_batch.pending) == 1
    repository.packages.mark_package_batch_completed(package, "batch-2", _TODAY)
    completed = repository.packages.package_work_plan(_TODAY, "batch-2")
    assert len(completed.completed) == 1


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("owner_id", "2"),
        ("owner_type", "orgs"),
        ("package_type", "npm"),
        ("owner", "Beta"),
        ("repo", "other"),
        ("package", "other"),
    ],
)
def test_global_plan_preserves_each_reference_field_and_pending_publication(
    tmp_path: Path, column: str, value: str
) -> None:
    """A completed reference cannot hide related unpublished package work."""

    repository = DatabaseRepositories(DatabaseSettings(tmp_path / "index.db"))
    published = PackageRef("1", "users", "container", "Alpha", "repo", "same")
    pending = replace(published, **{column: value})
    for package in (published, pending):
        repository.packages.write_package(PackageRecord(package, 1, 1, 1, 1, 1, _TODAY))
        repository.packages.mark_package_batch_completed(package, "batch", _TODAY)
    repository.packages.mark_package_publication_pending(pending, _TODAY)

    plan = repository.packages.package_work_plan("2026-06-11", "batch")

    assert len(plan.packages) == 2
    assert len(plan.completed) == 1
    assert len(plan.pending) == 1
    assert len(plan.packages) == len(plan.completed) + len(plan.pending)
    assert plan.completed[0].package_ref == published
    assert plan.pending[0].package_ref == pending


def test_date_based_plan_keeps_unpublished_types_and_latest_dates(
    tmp_path: Path,
) -> None:
    """Legacy date-based completion is exact and orders latest observations."""

    repository = DatabaseRepositories(DatabaseSettings(tmp_path / "index.db"))
    published = PackageRef("1", "users", "container", "Alpha", "repo", "same")
    unpublished = replace(published, package_type="npm")
    stale = replace(published, repo="stale")
    for package in (published, unpublished, stale):
        repository.packages.write_package(
            PackageRecord(package, 1, 1, 1, 1, 1, "2026-06-09")
        )
    repository.packages.write_package(PackageRecord(published, 2, 2, 2, 2, 2, _TODAY))
    repository.packages.write_package_pending_publication(
        PackageRecord(unpublished, 2, 2, 2, 2, 2, _TODAY)
    )

    plan = repository.packages.package_work_plan(_TODAY)

    assert len(plan.packages) == 3
    assert len(plan.completed) == 1
    assert len(plan.pending) == 2
    assert plan.packages[0].date == "2026-06-09"
    assert plan.completed[0].date == _TODAY
    assert plan.partially_updated_owners == ("Alpha",)
    assert plan.packages[0].package_ref == stale
    assert plan.completed[0].package_ref == published
    assert {item.package_ref for item in plan.pending} == {stale, unpublished}


def test_inventory_fallback_counts_references_without_changing_catalog_paths(
    tmp_path: Path,
) -> None:
    """Before catalog initialization, package totals retain type distinctions."""

    repository = DatabaseRepositories(DatabaseSettings(tmp_path / "index.db"))
    package = PackageRef("1", "users", "container", "Alpha", "repo", "same")
    for reference in (
        package,
        replace(package, package_type="npm"),
        replace(package, repo="other"),
    ):
        repository.packages.write_package(
            PackageRecord(reference, 1, 1, 1, 1, 1, _TODAY)
        )

    assert repository.packages.package_inventory() == PackageInventory(1, 2, 3)

    repository.catalog.initialize_package_catalog(
        (
            PackageCatalogPath("Alpha", "repo", "same"),
            PackageCatalogPath("Alpha", "other", "same"),
        ),
        "a" * 40,
        _TODAY,
    )
    assert repository.packages.package_inventory() == PackageInventory(1, 2, 2)
