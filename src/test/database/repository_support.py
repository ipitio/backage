"""Shared records and legacy-table helpers for database repository tests."""

from __future__ import annotations

import sqlite3

from bkg_py.database import PackageRef, VersionMetrics, VersionRecord

TODAY = "2026-06-10"
YESTERDAY = "2026-06-09"


def package(
    repo: str = "Libre-Closet",
    package_name: str = "libre-closet",
) -> PackageRef:
    """Build the canonical package identity used by repository tests."""

    return PackageRef(
        owner_id="69664378",
        owner_type="orgs",
        package_type="container",
        owner="Lazztech",
        repo=repo,
        package=package_name,
    )


def version(
    version_id: str,
    *,
    date: str = TODAY,
    downloads: int = 100,
) -> VersionRecord:
    """Build a version row with representative metrics."""

    return VersionRecord(
        version_id=version_id,
        name=f"sha256:{version_id}",
        metrics=VersionMetrics(
            size=123,
            downloads=downloads,
            downloads_month=10,
            downloads_week=5,
            downloads_day=1,
        ),
        date=date,
        tags="latest" if version_id == "2" else "",
    )


def legacy_table(package_ref: PackageRef) -> str:
    """Return the former per-package version table name."""

    return (
        f"versions_{package_ref.owner_type}_{package_ref.package_type}_"
        f"{package_ref.owner}_{package_ref.repo}_{package_ref.package}"
    )


def create_legacy_table(connection: sqlite3.Connection, table: str) -> None:
    """Create a legacy version table for compatibility-path tests."""

    quoted = table.replace('"', '""')
    columns = ", ".join(
        (
            "id text not null",
            "name text not null",
            "size integer not null",
            "downloads integer not null",
            "downloads_month integer not null",
            "downloads_week integer not null",
            "downloads_day integer not null",
            "date text not null",
            "tags text",
            "primary key (id, date)",
        )
    )
    connection.execute(f'create table "{quoted}" ({columns})')


def insert_legacy(
    connection: sqlite3.Connection,
    table: str,
    version_record: VersionRecord,
) -> None:
    """Insert a version into a legacy table."""

    quoted = table.replace('"', '""')
    metrics = version_record.metrics
    connection.execute(
        f"""
        insert into "{quoted}" (
            id, name, size, downloads, downloads_month, downloads_week,
            downloads_day, date, tags
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            version_record.version_id,
            version_record.name,
            metrics.size,
            metrics.downloads,
            metrics.downloads_month,
            metrics.downloads_week,
            metrics.downloads_day,
            version_record.date,
            version_record.tags,
        ),
    )
