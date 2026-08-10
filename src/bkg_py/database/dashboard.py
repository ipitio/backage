"""Bounded dashboard projection queries over the current package catalog."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass

from . import catalog
from .models import PackageInventory
from .support import DatabaseError

PACKAGE_TYPE_LIMIT = 16
DASHBOARD_QUERY_BUDGET_SECONDS = 30.0
_QUERY_PROGRESS_INSTRUCTIONS = 10_000
_FRESHNESS_BUCKETS = ("today", "days_1_7", "days_8_30", "days_31_plus", "unknown")
_PACKAGE_TYPES_SQL = """
    select case when package_type = '' then 'unknown' else package_type end,
           count(*) as packages
    from "bkg_package_catalog"
    group by case when package_type = '' then 'unknown' else package_type end
    order by packages desc, 1 collate binary
    limit ?
"""
_FRESHNESS_SQL = """
    select freshness, count(*)
    from (
        select case
            when observed_at = '' or date(observed_at) is null
                 or date(observed_at) > date(?) then 'unknown'
            when date(observed_at) = date(?) then 'today'
            when date(observed_at) >= date(?, '-7 days') then 'days_1_7'
            when date(observed_at) >= date(?, '-30 days') then 'days_8_30'
            else 'days_31_plus'
        end as freshness
        from "bkg_package_catalog"
    ) classified
    group by freshness
"""
_METRIC_COVERAGE_SQL = """
    select
        count(case when history.size >= 0 then 1 end),
        coalesce(sum(case when history.size >= 0 then history.size else 0 end), 0),
        count(case when history.downloads >= 0 then 1 end),
        coalesce(sum(case when history.downloads >= 0 then history.downloads else 0 end), 0),
        count(case when history.downloads_month >= 0 then 1 end),
        coalesce(sum(case when history.downloads_month >= 0 then history.downloads_month else 0 end), 0),
        count(case when history.downloads_week >= 0 then 1 end),
        coalesce(sum(case when history.downloads_week >= 0 then history.downloads_week else 0 end), 0),
        count(case when history.downloads_day >= 0 then 1 end),
        coalesce(sum(case when history.downloads_day >= 0 then history.downloads_day else 0 end), 0)
    from "bkg_package_catalog" catalog
    left join "bkg_package_history" history
      on history.owner_id = catalog.owner_id
     and history.owner_type = catalog.owner_type
     and history.package_type = catalog.package_type
     and history.owner = catalog.owner
     and history.repo = catalog.repo
     and history.package = catalog.package
     and history.date = catalog.observed_at
"""


@dataclass(frozen=True)
class DashboardDistributionItem:
    """One bounded package-type distribution row."""

    name: str
    packages: int


@dataclass(frozen=True)
class DashboardFreshnessBucket:
    """Current catalog packages in one fixed age bucket."""

    name: str
    packages: int


@dataclass(frozen=True)
class DashboardMetricCoverage:
    """Known package count and aggregate value for one metric."""

    name: str
    unit: str
    known_packages: int
    value: int


@dataclass(frozen=True)
class DashboardProjection:
    """One bounded, rotation-independent dashboard data snapshot."""

    inventory: PackageInventory
    resolved_packages: int
    package_types: tuple[DashboardDistributionItem, ...]
    other_packages: int
    freshness: tuple[DashboardFreshnessBucket, ...]
    metrics: tuple[DashboardMetricCoverage, ...]


def project(
    connection: sqlite3.Connection,
    today: str,
    *,
    clock: Callable[[], float] = time.monotonic,
    query_budget_seconds: float = DASHBOARD_QUERY_BUDGET_SECONDS,
    progress_instruction_interval: int = _QUERY_PROGRESS_INSTRUCTIONS,
) -> DashboardProjection:
    """Project current public-index analytics from one database snapshot."""

    if query_budget_seconds <= 0:
        raise ValueError("dashboard query budget must be positive")
    if progress_instruction_interval <= 0:
        raise ValueError("dashboard progress interval must be positive")
    deadline = clock() + query_budget_seconds
    connection.set_progress_handler(
        lambda: int(clock() >= deadline),
        progress_instruction_interval,
    )
    try:
        status = catalog.status(connection)
        if status is None:
            raise DatabaseError("package catalog is not initialized for dashboard data")
        inventory = status.inventory
        package_types = _package_types(connection)
        represented_packages = sum(item.packages for item in package_types)
        return DashboardProjection(
            inventory=inventory,
            resolved_packages=status.resolved_packages,
            package_types=package_types,
            other_packages=max(0, inventory.packages - represented_packages),
            freshness=_freshness(connection, today),
            metrics=_metric_coverage(connection),
        )
    except sqlite3.OperationalError as error:
        if "interrupted" in str(error).lower() and clock() >= deadline:
            raise DatabaseError(
                "dashboard projection exceeded its "
                f"{query_budget_seconds:g}s query budget"
            ) from error
        raise
    finally:
        connection.set_progress_handler(None, 0)


def _package_types(
    connection: sqlite3.Connection,
) -> tuple[DashboardDistributionItem, ...]:
    rows = connection.execute(_PACKAGE_TYPES_SQL, (PACKAGE_TYPE_LIMIT,)).fetchall()
    return tuple(DashboardDistributionItem(str(row[0]), int(row[1])) for row in rows)


def _freshness(
    connection: sqlite3.Connection,
    today: str,
) -> tuple[DashboardFreshnessBucket, ...]:
    rows = connection.execute(
        _FRESHNESS_SQL,
        (today, today, today, today),
    ).fetchall()
    counts = {str(row[0]): int(row[1]) for row in rows}
    return tuple(
        DashboardFreshnessBucket(name, counts.get(name, 0))
        for name in _FRESHNESS_BUCKETS
    )


def _metric_coverage(
    connection: sqlite3.Connection,
) -> tuple[DashboardMetricCoverage, ...]:
    row = connection.execute(_METRIC_COVERAGE_SQL).fetchone()
    if row is None:
        raise DatabaseError("dashboard metric coverage returned no row")
    values = tuple(int(value) for value in row)
    definitions = (
        ("size", "bytes"),
        ("downloads_total", "downloads"),
        ("downloads_month", "downloads"),
        ("downloads_week", "downloads"),
        ("downloads_day", "downloads"),
    )
    return tuple(
        DashboardMetricCoverage(name, unit, values[index], values[index + 1])
        for (name, unit), index in zip(
            definitions, range(0, len(values), 2), strict=True
        )
    )
