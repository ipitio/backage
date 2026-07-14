"""Conversions between typed database values and SQLite rows."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from .models import (
    PackageRecord,
    PackageRef,
    RankedPackage,
    VersionMetrics,
    VersionRecord,
)


def package_values(package: PackageRef) -> tuple[str, ...]:
    """Return the normalized identity columns for one package."""

    return (
        package.owner_id,
        package.owner_type,
        package.package_type,
        package.owner,
        package.repo,
        package.package,
    )


def package_sort_key(values: Sequence[str]) -> tuple[str, ...]:
    """Return the deterministic owner aggregate package ordering key."""

    owner_id, owner_type, package_type, owner, repo, package = values
    return owner, repo, package_type, package, owner_type, owner_id


def ranked_package(row: Sequence[Any]) -> RankedPackage:
    """Decode one ranked package query row."""

    package = PackageRef(
        owner_id=str(row[0]),
        owner_type=str(row[1]),
        package_type=str(row[2]),
        owner=str(row[3]),
        repo=str(row[4]),
        package=str(row[5]),
    )
    return RankedPackage(
        record=PackageRecord(
            package_ref=package,
            downloads=int(row[6]),
            downloads_month=int(row[7]),
            downloads_week=int(row[8]),
            downloads_day=int(row[9]),
            size=int(row[10]),
            date=str(row[11]),
        ),
        owner_rank=int(row[12]),
        repo_rank=int(row[13]),
    )


def normalized_version_values(
    package: PackageRef,
    version: VersionRecord,
) -> tuple[str | int, ...]:
    """Return one normalized version insert row."""

    return (*package_values(package), *legacy_version_values(version))


def legacy_version_values(version: VersionRecord) -> tuple[str | int, ...]:
    """Return one legacy-compatible version insert row."""

    metrics = version.metrics
    return (
        version.version_id,
        version.name,
        metrics.size,
        metrics.downloads,
        metrics.downloads_month,
        metrics.downloads_week,
        metrics.downloads_day,
        version.date,
        version.tags,
    )


def version_records(rows: Iterable[Sequence[Any]]) -> tuple[VersionRecord, ...]:
    """Decode multiple version query rows."""

    return tuple(version_record(row) for row in rows)


def version_record(row: Sequence[Any]) -> VersionRecord:
    """Decode one version query row."""

    return VersionRecord(
        version_id=str(row[0]),
        name=str(row[1]),
        metrics=VersionMetrics(
            size=int(row[2]),
            downloads=int(row[3]),
            downloads_month=int(row[4]),
            downloads_week=int(row[5]),
            downloads_day=int(row[6]),
        ),
        date=str(row[7]),
        tags="" if row[8] is None else str(row[8]),
    )
