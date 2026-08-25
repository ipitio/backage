"""Tests for bounded database storage and finalization measurements."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

from bkg_py.database.composition import DatabaseRepositories
from bkg_py.database.maintenance.metrics import (
    DatabaseMetricSample,
    DatabaseWriteCounts,
)
from bkg_py.database.models import PackageRecord, VersionStage
from bkg_py.database.settings import DatabaseSettings

from ..repository_support import legacy_table, package, version


def test_storage_metrics_bound_dates_and_group_legacy_objects(tmp_path: Path) -> None:
    """Measurements stay compact even with old dates and legacy tables."""

    path = tmp_path / "index.db"
    repository = DatabaseRepositories(DatabaseSettings(path))
    package_ref = package()
    for day in range(35):
        observed_on = (date(2026, 5, 1) + timedelta(days=day)).isoformat()
        repository.packages.write_package(
            PackageRecord(package_ref, day, day, day, day, day, observed_on)
        )
    repository.packages.flush_version_stage(
        VersionStage(
            package_ref,
            legacy_table(package_ref),
            False,
            (version("one"), version("two")),
        )
    )
    with sqlite3.connect(path) as connection:
        connection.execute('create table "versions_orgs_container_A_a_a" (id text)')
        connection.execute('create table "versions_orgs_container_B_b_b" (id text)')

    storage = repository.metrics.database_storage_metrics()

    assert storage.pages.physical_bytes > 0
    assert storage.pages.logical_bytes > 0
    assert storage.pages.page_count >= storage.pages.freelist_pages
    assert storage.package_rows == 35
    assert storage.version_rows == 2
    assert len(storage.package_rows_by_date) == 33
    assert storage.package_rows_by_date[0].date.startswith("<")
    assert storage.package_rows_by_date[0].rows == 3
    if storage.objects:
        legacy_objects = {
            item.name: item.objects
            for item in storage.objects
            if item.name.startswith("legacy-version-")
        }
        assert legacy_objects["legacy-version-tables"] == 2
    assert repository.metrics.database_write_counts() == DatabaseWriteCounts(35, 2)


def test_daily_samples_accumulate_writes_and_keep_latest_storage(
    tmp_path: Path,
) -> None:
    """Repeated finalization creates one sample per day, not one row per run."""

    repository = DatabaseRepositories(DatabaseSettings(tmp_path / "index.db"))
    repository.kernel.ensure_schema()
    storage = repository.metrics.database_storage_metrics()
    repository.metrics.record_database_metric_sample(
        DatabaseMetricSample(
            "2026-07-05",
            1,
            storage,
            DatabaseWriteCounts(2, 5),
            100,
            0,
            0,
            storage.pages.physical_bytes,
        )
    )
    latest_pages = replace(
        storage.pages,
        physical_bytes=storage.pages.physical_bytes + storage.pages.page_size,
    )
    latest_storage = replace(
        storage,
        pages=latest_pages,
        package_rows=3,
        version_rows=7,
    )
    repository.metrics.record_database_metric_sample(
        DatabaseMetricSample(
            "2026-07-05",
            1,
            latest_storage,
            DatabaseWriteCounts(3, 7),
            200,
            1,
            50,
            latest_storage.pages.physical_bytes,
        )
    )
    repository.metrics.record_database_metric_sample(
        DatabaseMetricSample(
            "2026-07-06",
            1,
            latest_storage,
            DatabaseWriteCounts(),
            latest_storage.pages.physical_bytes,
            0,
            0,
            latest_storage.pages.physical_bytes,
        )
    )

    samples = repository.metrics.database_metric_samples()

    assert len(samples) == 2
    first = samples[0]
    assert first.sample_date == "2026-07-05"
    assert first.run_count == 2
    assert first.storage.pages.physical_bytes == latest_storage.pages.physical_bytes
    assert first.storage.package_rows == 3
    assert first.storage.version_rows == 7
    assert first.writes == DatabaseWriteCounts(5, 12)
    assert first.maximum_pre_rotation_bytes == 200
    assert first.rotation_count == 1
    assert first.rotation_archive_bytes == 50
    assert samples[1].sample_date == "2026-07-06"
