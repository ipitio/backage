"""Finalize database snapshots and generated run summaries in one operation."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Protocol

from ..database.maintenance.metrics import (
    DatabaseMetricSample,
    DatabaseStorageMetrics,
    DatabaseWriteCounts,
)
from ..database.models import (
    DatabaseRotationEvent,
    PackageInventory,
)
from ..publication.release import release_tag as release_tag_for_date
from ..runtime_names import StateKey
from ..snapshots import SnapshotError, SnapshotRotationResult
from ..state import StateStore
from .publication import RunPublicationRequest

MessageSink = Callable[[str], None]
StopCheck = Callable[[], None]


def _utc_now() -> datetime:
    return datetime.now(UTC)


class LegacyCleanupRepository(Protocol):  # pylint: disable=too-few-public-methods
    """Package-history cleanup needed when an oversized snapshot rotates."""

    def cleanup_replaced_legacy_tables(
        self,
        *,
        since: str,
        prune_normalized: bool = False,
        vacuum: bool = False,
    ) -> int:
        """Prune old normalized rows and replaced legacy tables."""

        raise NotImplementedError


class MetricsRepository(Protocol):
    """Database measurements needed during snapshot finalization."""

    def database_storage_metrics(self) -> DatabaseStorageMetrics:
        """Capture bounded storage measurements."""

        raise NotImplementedError

    def database_write_counts(self) -> DatabaseWriteCounts:
        """Return normalized writes by this process."""

        raise NotImplementedError

    def record_database_metric_sample(self, sample: DatabaseMetricSample) -> None:
        """Persist one compact daily finalization sample."""

        raise NotImplementedError


class RotationEventRepository(Protocol):
    """Durable rotation events used by finalization and release publication."""

    def record_database_rotation(self, event: DatabaseRotationEvent) -> None:
        """Persist one completed database rotation."""

        raise NotImplementedError

    def database_rotations_for_release(
        self,
        release_tag: str,
    ) -> tuple[DatabaseRotationEvent, ...]:
        """Return durable rotation events for one release."""

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
        rotation_stamp: str,
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

    packages: LegacyCleanupRepository
    metrics: MetricsRepository
    rotations: RotationEventRepository
    snapshots: SnapshotFinalizer
    publisher: RunSummaryPublisher
    state: StateStore


@dataclass(frozen=True)
class RunFinalizationExecution:
    """Runtime callbacks used during finalization."""

    check_stop: StopCheck
    progress: MessageSink
    now: Callable[[], datetime] = _utc_now


@dataclass(frozen=True)
class _PreparedSnapshot:
    rotated: bool
    path: Path


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

        run_date = date.fromisoformat(request.publication.today)
        current_release_tag = release_tag_for_date(run_date)
        prepared: _PreparedSnapshot | None = None
        if request.prepare_snapshot:
            prepared = self._prepare_snapshot(request, current_release_tag)

        self.execution.progress("Hydrating templates and cleaning up...")
        rotation_events = self.services.rotations.database_rotations_for_release(
            current_release_tag
        )
        inventory = self.services.publisher.publish(
            replace(request.publication, rotation_events=rotation_events)
        )
        self.execution.progress("Done!")
        return RunFinalizationResult(
            prepared.rotated if prepared is not None else False,
            prepared.path if prepared is not None else None,
            inventory,
        )

    def _prepare_snapshot(
        self,
        request: RunFinalizationRequest,
        current_release_tag: str,
    ) -> _PreparedSnapshot:
        self.services.state.set(StateKey.OUT, _line_count(request.optout_file))
        self.execution.progress("Preparing the database snapshot...")
        self.execution.check_stop()
        self.services.snapshots.checkpoint_database()
        writes = self.services.metrics.database_write_counts()
        before = self.services.metrics.database_storage_metrics()
        rotated, archive_bytes, after = self._rotate_if_needed(
            request,
            current_release_tag,
            before,
        )
        sample = DatabaseMetricSample(
            sample_date=request.publication.today,
            run_count=1,
            storage=after,
            writes=writes,
            maximum_pre_rotation_bytes=before.pages.physical_bytes,
            rotation_count=int(rotated),
            rotation_archive_bytes=archive_bytes,
            snapshot_bytes=after.pages.physical_bytes,
        )
        self.services.metrics.record_database_metric_sample(sample)
        snapshot = self.services.snapshots.prepare_database_snapshot()
        self._report_database_metrics(
            sample,
            snapshot_bytes=_path_size(
                snapshot,
                fallback=after.pages.physical_bytes,
            ),
        )
        self.execution.progress("Prepared the database snapshot")
        return _PreparedSnapshot(rotated, snapshot)

    def _rotate_if_needed(
        self,
        request: RunFinalizationRequest,
        current_release_tag: str,
        before: DatabaseStorageMetrics,
    ) -> tuple[bool, int, DatabaseStorageMetrics]:
        if before.pages.physical_bytes < request.rotation_threshold_bytes:
            return False, 0, before
        self.execution.progress("Rotating the database...")
        rotated_at, rotation_stamp = _rotation_time(self.execution.now())
        rotation = self.services.snapshots.rotate_database_if_needed(
            lambda: self.services.packages.cleanup_replaced_legacy_tables(
                since=request.batch_first_started,
                prune_normalized=True,
                vacuum=True,
            ),
            threshold_bytes=request.rotation_threshold_bytes,
            rotation_stamp=rotation_stamp,
        )
        archive_bytes = self._record_rotation(
            request,
            current_release_tag,
            rotated_at,
            rotation,
        )
        after = self.services.metrics.database_storage_metrics()
        self.execution.progress("Rotated the database")
        return rotation.rotated, archive_bytes, after

    def _record_rotation(
        self,
        request: RunFinalizationRequest,
        current_release_tag: str,
        rotated_at: str,
        rotation: SnapshotRotationResult,
    ) -> int:
        if not rotation.rotated:
            return 0
        if rotation.archive is None:
            raise SnapshotError("database rotation did not produce a retained archive")
        archive_bytes = rotation.compressed_bytes or _path_size(rotation.archive)
        self.services.rotations.record_database_rotation(
            DatabaseRotationEvent(
                release_tag=current_release_tag,
                rotated_at=rotated_at,
                archive_name=rotation.archive.name,
                source_bytes=rotation.source_bytes,
                compressed_bytes=archive_bytes,
                retained_since=request.batch_first_started,
            )
        )
        return archive_bytes

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


def _rotation_time(value: datetime) -> tuple[str, str]:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("rotation timestamp must include a timezone")
    utc_value = value.astimezone(UTC)
    rotated_at = utc_value.isoformat(timespec="microseconds").replace("+00:00", "Z")
    archive_stamp = utc_value.strftime("%Y.%m.%dT%H.%M.%S.%fZ")
    return rotated_at, archive_stamp
