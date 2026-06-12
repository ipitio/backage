"""Typed values stored in or loaded for the package metadata database."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .database_support import (
    DatabaseError,
    load_object,
    optional_text,
    required_int,
    required_string,
    required_text,
)


@dataclass(frozen=True)
class PackageRef:
    """Columns that identify one package across normalized tables."""

    owner_id: str
    owner_type: str
    package_type: str
    owner: str
    repo: str
    package: str


@dataclass(frozen=True)
class OwnerRecord:
    """One normalized owner scan record."""

    owner_id: str
    owner: str
    date: str


@dataclass(frozen=True)
class PackageRecord:
    """One normalized package metadata record."""

    package_ref: PackageRef
    downloads: int
    downloads_month: int
    downloads_week: int
    downloads_day: int
    size: int
    date: str


@dataclass(frozen=True)
class RankedPackage:
    """A latest package row with owner-wide and repository-local ranks."""

    record: PackageRecord
    owner_rank: int
    repo_rank: int


@dataclass(frozen=True)
class VersionMetrics:
    """Raw size and download counters for one version."""

    size: int
    downloads: int
    downloads_month: int
    downloads_week: int
    downloads_day: int


@dataclass(frozen=True)
class VersionRecord:
    """One normalized package-version record."""

    version_id: str
    name: str
    metrics: VersionMetrics
    date: str
    tags: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> VersionRecord:
        """Validate and decode one staged JSON record."""

        return cls(
            version_id=required_text(value, "id"),
            name=required_text(value, "name"),
            metrics=VersionMetrics(
                size=required_int(value, "size"),
                downloads=required_int(value, "downloads"),
                downloads_month=required_int(value, "downloads_month"),
                downloads_week=required_int(value, "downloads_week"),
                downloads_day=required_int(value, "downloads_day"),
            ),
            date=required_text(value, "date"),
            tags=optional_text(value, "tags"),
        )


@dataclass(frozen=True)
class VersionSource:
    """Version rows and the normalized or legacy source that supplied them."""

    source: str
    rows: tuple[VersionRecord, ...]


@dataclass(frozen=True)
class PackageSnapshot:
    """One ranked package and the version source used to render it."""

    package: RankedPackage
    versions: VersionSource


@dataclass(frozen=True)
class VersionLimitEstimate:
    """Inputs for estimating a bounded owner aggregate version slice."""

    repo: str | None
    target_bytes: int
    headroom_percent: int
    fallback_limit: int


@dataclass(frozen=True)
class VersionStage:
    """A complete package identity and its staged version rows."""

    package_ref: PackageRef
    legacy_table: str
    write_legacy: bool
    rows: tuple[VersionRecord, ...]

    @classmethod
    def load(cls, directory: Path) -> VersionStage:
        """Load a manifest and all row files without modifying the stage."""

        manifest = load_object(directory / "manifest.json")
        package_ref = PackageRef(
            owner_id=required_string(manifest, "owner_id"),
            owner_type=required_text(manifest, "owner_type"),
            package_type=required_text(manifest, "package_type"),
            owner=required_text(manifest, "owner"),
            repo=required_text(manifest, "repo"),
            package=required_text(manifest, "package"),
        )
        legacy_table = required_text(manifest, "legacy_table")
        write_legacy = manifest.get("write_legacy")
        if not isinstance(write_legacy, bool):
            raise DatabaseError("stage field 'write_legacy' must be a boolean")

        row_paths = sorted(directory.glob("row.*.json"))
        rows = tuple(
            VersionRecord.from_mapping(load_object(path)) for path in row_paths
        )
        return cls(
            package_ref=package_ref,
            legacy_table=legacy_table,
            write_legacy=write_legacy,
            rows=rows,
        )
