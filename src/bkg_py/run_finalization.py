"""Finalize database snapshots and generated run summaries in one operation."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

from .database import (
    DatabaseMetricSample,
    DatabaseStorageMetrics,
    DatabaseWriteCounts,
    PackageInventory,
)
from .run_publication import RunPublicationRequest
from .snapshots import SnapshotRotationResult
from .state import StateStore

MessageSink = Callable[[str], None]
StopCheck = Callable[[], None]


class RotationRepository(Protocol):  # pylint: disable=too-few-public-methods
    """Database cleanup needed when an oversized snapshot rotates."""

    def cleanup_replaced_legacy_tables(
        self,
        *,
        since: str,
        prune_normalized: bool = False,
        vacuum: bool = False,
    ) -> int:
        """Prune old normalized rows and replaced legacy tables."""

        raise NotImplementedError

    def database_storage_metrics(self) -> DatabaseStorageMetrics:
        """Capture bounded storage measurements."""

        raise NotImplementedError

    def database_write_counts(self) -> DatabaseWriteCounts:
        """Return normalized writes by this process."""

        raise NotImplementedError

    def record_database_metric_sample(self, sample: DatabaseMetricSample) -> None:
        """Persist one compact daily finalization sample."""

        raise NotImplementedError


class SnapshotFinalizer(Protocol):
    """Snapshot operations required by run finalization."""

    def checkpoint_database(self) -> None:
        """Checkpoint the live database."""

        raise NotImplementedError

    def rotate_database_if_needed(
        self,
        prune_database: Callable[[], object],
        *,
        threshold_bytes: int,
        date_stamp: str,
    ) -> SnapshotRotationResult:
        """Rotate an oversized database and return the result."""

        raise NotImplementedError

    def prepare_database_snapshot(self) -> Path:
        """Publish the current local database archive."""

        raise NotImplementedError


class RunSummaryPublisher(Protocol):  # pylint: disable=too-few-public-methods
    """Final generated-summary operation used after snapshot preparation."""

    def publish(self, request: RunPublicationRequest) -> PackageInventory:
        """Publish final source and index summaries."""

        raise NotImplementedError


@dataclass(frozen=True)
class RunFinalizationRequest:
    """Inputs controlling one final snapshot and summary publication."""

    publication: RunPublicationRequest
    optout_file: Path
    batch_first_started: str
    prepare_snapshot: bool
    rotation_threshold_bytes: int


@dataclass(frozen=True)
class RunFinalizationResult:
    """Artifacts and inventory produced by finalization."""

    rotated: bool
    snapshot: Path | None
    inventory: PackageInventory


@dataclass(frozen=True)
class RunFinalizationServices:
    """Stateful operations used during finalization."""

    repository: RotationRepository
    snapshots: SnapshotFinalizer
    publisher: RunSummaryPublisher
    state: StateStore


@dataclass(frozen=True)
class RunFinalizationExecution:
    """Runtime callbacks used during finalization."""

    check_stop: StopCheck
    progress: MessageSink


class RunFinalizationService:  # pylint: disable=too-few-public-methods
    """Order durable snapshot work before generated summary publication."""

    def __init__(
        self,
        services: RunFinalizationServices,
        execution: RunFinalizationExecution,
    ) -> None:
        self.services = services
        self.execution = execution

    def finalize(self, request: RunFinalizationRequest) -> RunFinalizationResult:
        """Prepare recoverable state, then publish final generated summaries."""

        if request.rotation_threshold_bytes <= 0:
            raise ValueError("snapshot rotation threshold must be positive")

        rotated = False
        snapshot: Path | None = None
        if request.prepare_snapshot:
            self.services.state.set("BKG_OUT", _line_count(request.optout_file))
            self.execution.progress("Preparing the database snapshot...")
            self.execution.check_stop()
            self.services.snapshots.checkpoint_database()
            writes = self.services.repository.database_write_counts()
            before = self.services.repository.database_storage_metrics()
            after = before
            rotation_archive_bytes = 0
            if before.pages.physical_bytes >= request.rotation_threshold_bytes:
                self.execution.progress("Rotating the database...")
                rotation = self.services.snapshots.rotate_database_if_needed(
                    lambda: self.services.repository.cleanup_replaced_legacy_tables(
                        since=request.batch_first_started,
                        prune_normalized=True,
                        vacuum=True,
                    ),
                    threshold_bytes=request.rotation_threshold_bytes,
                    date_stamp=request.publication.today.replace("-", "."),
                )
                rotated = rotation.rotated
                rotation_archive_bytes = _path_size(rotation.archive)
                after = self.services.repository.database_storage_metrics()
                self.execution.progress("Rotated the database")
            sample = DatabaseMetricSample(
                sample_date=request.publication.today,
                run_count=1,
                storage=after,
                writes=writes,
                maximum_pre_rotation_bytes=before.pages.physical_bytes,
                rotation_count=int(rotated),
                rotation_archive_bytes=rotation_archive_bytes,
                snapshot_bytes=after.pages.physical_bytes,
            )
            self.services.repository.record_database_metric_sample(sample)
            snapshot = self.services.snapshots.prepare_database_snapshot()
            self._report_database_metrics(
                sample,
                snapshot_bytes=(
                    _path_size(snapshot, fallback=after.pages.physical_bytes)
                ),
            )
            self.execution.progress("Prepared the database snapshot")

        self.execution.progress("Hydrating templates and cleaning up...")
        inventory = self.services.publisher.publish(
            replace(request.publication, rotated=rotated)
        )
        self.execution.progress("Done!")
        return RunFinalizationResult(rotated, snapshot, inventory)

    def _report_database_metrics(
        self,
        sample: DatabaseMetricSample,
        *,
        snapshot_bytes: int,
    ) -> None:
        storage = sample.storage
        pages = storage.pages
        self.execution.progress(
            "Database finalization telemetry: "
            + _compact_json(
                {
                    "archive_bytes": sample.rotation_archive_bytes,
                    "freelist_pages": pages.freelist_pages,
                    "logical_bytes": pages.logical_bytes,
                    "package_rows": storage.package_rows,
                    "package_rows_written": sample.writes.package_rows,
                    "page_count": pages.page_count,
                    "page_size": pages.page_size,
                    "physical_bytes": pages.physical_bytes,
                    "pre_rotation_bytes": sample.maximum_pre_rotation_bytes,
                    "rotated": bool(sample.rotation_count),
                    "snapshot_bytes": snapshot_bytes,
                    "version_rows": storage.version_rows,
                    "version_rows_written": sample.writes.version_rows,
                }
            )
        )
        self.execution.progress(
            "Database object telemetry: "
            + _compact_json(
                {
                    f"{item.kind}:{item.name}": {
                        "bytes": item.bytes,
                        "objects": item.objects,
                    }
                    for item in storage.objects
                }
            )
        )
        self.execution.progress(
            "Database date telemetry: "
            + _compact_json(
                {
                    "packages": [
                        [item.date, item.rows] for item in storage.package_rows_by_date
                    ],
                    "versions": [
                        [item.date, item.rows] for item in storage.version_rows_by_date
                    ],
                }
            )
        )


def _line_count(path: Path) -> int:
    try:
        return len(path.read_text(encoding="utf-8").splitlines())
    except FileNotFoundError:
        return 0


def _path_size(path: Path | None, *, fallback: int = 0) -> int:
    if path is None:
        return fallback
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return fallback


def _compact_json(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)
