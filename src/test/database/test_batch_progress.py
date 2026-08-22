"""Tests for durable per-package batch generation progress."""

from __future__ import annotations

from pathlib import Path

from bkg_py.database.composition import DatabaseRepositories
from bkg_py.database.models import PackageRecord, PackageRef
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
