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
class OwnerScanPackage:
    """One package identity observed during a complete owner listing scan."""

    owner_type: str
    package_type: str
    repo: str
    package: str


def load_owner_scan_packages(path: Path) -> tuple[OwnerScanPackage, ...]:
    """Load tab-separated owner scan package identities from a file."""

    field_count = 4
    packages: list[OwnerScanPackage] = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        if not line:
            continue
        fields = line.split("\t")
        if len(fields) != field_count or not all(fields):
            raise DatabaseError(f"invalid owner scan package at {path}:{line_number}")
        packages.append(OwnerScanPackage(*fields))
    return tuple(packages)


@dataclass(frozen=True)
class OwnerScanCursor:
    """The durable page cursor for one resumable owner listing scan."""

    marker: str
    next_page: int
    resumed: bool


@dataclass(frozen=True)
class OwnerScanStart:
    """Inputs for resuming or replacing one owner listing scan."""

    owner_id: str
    owner: str
    batch_marker: str
    started_at: int
    legacy_marker: str | None = None
    legacy_page: int | None = None


@dataclass(frozen=True)
class OwnerScanPage:
    """A page operation against one active owner listing scan."""

    owner_id: str
    marker: str
    page: int
    updated_at: int


@dataclass(frozen=True)
class OwnerScanWorkSelection:
    """Observed packages to compare with current batch state."""

    owner_id: str
    owner: str
    packages: tuple[OwnerScanPackage, ...]
    since: str


@dataclass(frozen=True)
class OwnerRefreshPlan:
    """Current-batch package work for one partially refreshed owner."""

    partially_updated: bool
    packages: tuple[OwnerScanPackage, ...]

    @property
    def pending_count(self) -> int:
        """Return the number of packages still requiring refresh work."""

        return len(self.packages)


@dataclass(frozen=True)
class OwnerScanResult:
    """The reconciliation result for one completed owner listing scan."""

    removed: tuple[PackageRef, ...]
    pending: tuple[OwnerScanPackage, ...]
    retry_after: int

    @property
    def pending_count(self) -> int:
        """Return the number of observed packages still awaiting publication."""

        return len(self.pending)


@dataclass(frozen=True)
class OwnerIdentityCleanup:
    """Superseded owner identities and orphaned generated package paths."""

    alias_ids: tuple[str, ...]
    orphaned_packages: tuple[PackageRef, ...]


@dataclass(frozen=True)
class OwnerScanFailure:
    """One failed owner scan or direct refresh attempt."""

    owner_id: str
    owner: str
    marker: str | None
    error: str
    failed_at: int


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
class PackageWorkItem:
    """Shell-compatible package identity and latest update date."""

    owner_id: str
    owner: str
    repo: str
    package: str
    date: str


@dataclass(frozen=True)
class PackageWorkPlan:
    """Current package work and owner ordering captured from one snapshot."""

    packages: tuple[PackageWorkItem, ...]
    completed: tuple[PackageWorkItem, ...]
    pending: tuple[PackageWorkItem, ...]
    owners: tuple[str, ...]
    scanned_without_packages: tuple[str, ...]

    @property
    def updated_owners(self) -> tuple[str, ...]:
        """Return owners with at least one completed package in plan order."""

        return _unique_work_owners(self.completed)

    @property
    def pending_owners(self) -> tuple[str, ...]:
        """Return owners with at least one pending package in plan order."""

        return _unique_work_owners(self.pending)

    @property
    def partially_updated_owners(self) -> tuple[str, ...]:
        """Return pending owners that also have completed package work."""

        updated = set(self.updated_owners)
        return tuple(owner for owner in self.pending_owners if owner in updated)

    @property
    def stale_owners(self) -> tuple[str, ...]:
        """Return pending owners with no completed package work."""

        updated = set(self.updated_owners)
        return tuple(owner for owner in self.pending_owners if owner not in updated)


def _unique_work_owners(items: tuple[PackageWorkItem, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(item.owner for item in items))


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
