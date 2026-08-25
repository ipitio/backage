"""Tests for bounded dashboard queries over the current package catalog."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import bkg_py.database.catalog.dashboard as dashboard_queries
from bkg_py.database.catalog.dashboard import (
    DashboardDistributionItem,
    DashboardFreshnessBucket,
    DashboardMetricCoverage,
)
from bkg_py.database.composition import DatabaseRepositories
from bkg_py.database.models import (
    PackageCatalogPath,
    PackageInventory,
    PackageRecord,
    PackageRef,
)
from bkg_py.database.settings import DatabaseSettings
from bkg_py.database.support import DatabaseError
from bkg_py.database.values import package_values

from ..repository_support import (
    TODAY,
    create_normalized_package_table,
    package,
)


def _record(
    package_ref: PackageRef,
    observed_at: str,
    metrics: tuple[int, int, int, int, int],
) -> PackageRecord:
    downloads, month, week, day, size = metrics
    return PackageRecord(package_ref, downloads, month, week, day, size, observed_at)


def _path(package_ref: PackageRef) -> PackageCatalogPath:
    return PackageCatalogPath(
        package_ref.owner,
        package_ref.repo,
        package_ref.package,
    )


def test_dashboard_requires_a_ready_catalog_and_supports_an_empty_one(
    tmp_path: Path,
) -> None:
    """An incomplete seed cannot masquerade as an empty public index."""

    repository = DatabaseRepositories(DatabaseSettings(tmp_path / "index.db"))

    with pytest.raises(DatabaseError, match="catalog is not initialized"):
        repository.dashboard.dashboard_projection(TODAY)

    repository.catalog.initialize_package_catalog((), "a" * 40, TODAY)

    projection = repository.dashboard.dashboard_projection(TODAY)

    assert projection.inventory == PackageInventory(0, 0, 0)
    assert projection.resolved_packages == 0
    assert projection.package_types == ()
    assert projection.other_packages == 0
    assert projection.freshness == tuple(
        DashboardFreshnessBucket(name, 0)
        for name in (
            "today",
            "days_1_7",
            "days_8_30",
            "days_31_plus",
            "unknown",
        )
    )
    assert all(
        metric.known_packages == metric.value == 0 for metric in projection.metrics
    )


def test_dashboard_projects_exact_current_coverage_and_freshness(
    tmp_path: Path,
) -> None:
    """Current observations use catalog totals and fixed unknown semantics."""

    repository = DatabaseRepositories(DatabaseSettings(tmp_path / "index.db"))
    records = (
        _record(
            PackageRef("1", "users", "container", "Alpha", "one", "one"),
            TODAY,
            (100, 10, 5, 1, 100),
        ),
        _record(
            PackageRef("1", "users", "container", "Alpha", "two", "two"),
            "2026-06-09",
            (-1, -1, -1, -1, -1),
        ),
        _record(
            PackageRef("2", "orgs", "npm", "Beta", "three", "three"),
            "2026-05-20",
            (30, 3, 2, 1, -1),
        ),
        _record(
            PackageRef("3", "users", "maven", "Gamma", "four", "four"),
            "2026-05-01",
            (-1, -1, -1, -1, 400),
        ),
    )
    for record in records:
        repository.packages.write_package(record)
    repository.catalog.initialize_package_catalog(
        (
            *(_path(record.package_ref) for record in records),
            PackageCatalogPath("Tree", "only", "only"),
        ),
        "b" * 40,
        TODAY,
    )

    projection = repository.dashboard.dashboard_projection(TODAY)

    assert projection.inventory == PackageInventory(4, 5, 5)
    assert projection.resolved_packages == 4
    assert projection.package_types == (
        DashboardDistributionItem("container", 2),
        DashboardDistributionItem("maven", 1),
        DashboardDistributionItem("npm", 1),
        DashboardDistributionItem("unknown", 1),
    )
    assert projection.other_packages == 0
    assert projection.freshness == (
        DashboardFreshnessBucket("today", 1),
        DashboardFreshnessBucket("days_1_7", 1),
        DashboardFreshnessBucket("days_8_30", 1),
        DashboardFreshnessBucket("days_31_plus", 1),
        DashboardFreshnessBucket("unknown", 1),
    )
    assert projection.metrics == (
        DashboardMetricCoverage("size", "bytes", 2, 500),
        DashboardMetricCoverage("downloads_total", "downloads", 2, 130),
        DashboardMetricCoverage("downloads_month", "downloads", 2, 13),
        DashboardMetricCoverage("downloads_week", "downloads", 2, 7),
        DashboardMetricCoverage("downloads_day", "downloads", 2, 2),
    )


def test_dashboard_bounds_package_types_with_deterministic_ties(
    tmp_path: Path,
) -> None:
    """Equal package-type counts use binary name order and a bounded tail."""

    repository = DatabaseRepositories(DatabaseSettings(tmp_path / "index.db"))
    records = tuple(
        _record(
            PackageRef(
                str(index),
                "users",
                f"type-{index:02}",
                f"Owner{index:02}",
                "repo",
                "package",
            ),
            TODAY,
            (1, 1, 1, 1, 1),
        )
        for index in range(18)
    )
    for record in records:
        repository.packages.write_package(record)
    repository.catalog.initialize_package_catalog(
        tuple(_path(record.package_ref) for record in records),
        "c" * 40,
        TODAY,
    )

    projection = repository.dashboard.dashboard_projection(TODAY)

    assert tuple(item.name for item in projection.package_types) == tuple(
        f"type-{index:02}" for index in range(16)
    )
    assert all(item.packages == 1 for item in projection.package_types)
    assert projection.other_packages == 2


def test_dashboard_catalog_survives_history_rotation_with_unknown_metrics(
    tmp_path: Path,
) -> None:
    """Rotation preserves current paths while expired observations become unknown."""

    repository = DatabaseRepositories(DatabaseSettings(tmp_path / "index.db"))
    record = _record(package(), "2026-05-01", (10, 5, 2, 1, 100))
    repository.packages.write_package(record)
    repository.catalog.initialize_package_catalog(
        (_path(record.package_ref), PackageCatalogPath("Tree", "only", "only")),
        "d" * 40,
        TODAY,
    )

    repository.packages.cleanup_replaced_legacy_tables(
        since=TODAY,
        prune_normalized=True,
    )
    projection = repository.dashboard.dashboard_projection(TODAY)

    assert projection.inventory == PackageInventory(2, 2, 2)
    assert projection.resolved_packages == 1
    assert all(
        metric.known_packages == metric.value == 0 for metric in projection.metrics
    )


def test_dashboard_reads_current_metrics_from_a_legacy_compatibility_view(
    tmp_path: Path,
) -> None:
    """A large fork can publish coverage before its lazy migration completes."""

    path = tmp_path / "index.db"
    package_ref = package()
    with sqlite3.connect(path) as connection:
        create_normalized_package_table(connection)
        connection.execute(
            "insert into packages values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (*package_values(package_ref), 10, 5, 2, 1, 123, TODAY),
        )
    repository = DatabaseRepositories(DatabaseSettings(path))
    repository.catalog.initialize_package_catalog(
        (_path(package_ref),),
        "e" * 40,
        TODAY,
    )

    projection = repository.dashboard.dashboard_projection(TODAY)

    assert projection.resolved_packages == 1
    assert projection.metrics[0] == DashboardMetricCoverage("size", "bytes", 1, 123)
    assert projection.metrics[1] == DashboardMetricCoverage(
        "downloads_total",
        "downloads",
        1,
        10,
    )


def test_dashboard_query_budget_interrupts_and_clears_its_handler(
    tmp_path: Path,
) -> None:
    """Optional analytics cannot hold final snapshot publication indefinitely."""

    path = tmp_path / "index.db"
    repository = DatabaseRepositories(DatabaseSettings(path))
    repository.catalog.initialize_package_catalog((), "f" * 40, TODAY)
    clock_calls = 0

    def clock() -> float:
        nonlocal clock_calls
        clock_calls += 1
        return 0.0 if clock_calls == 1 else 31.0

    with sqlite3.connect(path) as connection:
        with pytest.raises(DatabaseError, match=r"exceeded its 30s query budget"):
            dashboard_queries.project(
                connection,
                TODAY,
                clock=clock,
                progress_instruction_interval=1,
            )
        assert connection.execute("select 1").fetchone() == (1,)
