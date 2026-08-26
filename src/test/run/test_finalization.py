"""Tests for combined snapshot and run-summary finalization."""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest

from bkg_py.database.maintenance.metrics import (
    DatabaseDateRows,
    DatabaseMetricSample,
    DatabaseObjectBytes,
    DatabasePageMetrics,
    DatabaseStorageMetrics,
    DatabaseWriteCounts,
)
from bkg_py.database.models import (
    DatabaseRotationEvent,
    PackageInventory,
)
from bkg_py.run.finalization import (
    RunFinalizationExecution,
    RunFinalizationRequest,
    RunFinalizationService,
    RunFinalizationServices,
)
from bkg_py.run.publication import (
    RunPublicationIdentity,
    RunPublicationPaths,
    RunPublicationRequest,
)
from bkg_py.snapshots import SnapshotError, SnapshotRotationResult
from bkg_py.state import StateStore


@dataclass
class _Repository:
    physical_bytes: int = 101
    cleanups: list[tuple[str, bool, bool]] = field(
        default_factory=list[tuple[str, bool, bool]]
    )
    samples: list[DatabaseMetricSample] = field(
        default_factory=list[DatabaseMetricSample]
    )
    rotations: list[DatabaseRotationEvent] = field(
        default_factory=list[DatabaseRotationEvent]
    )

    def cleanup_replaced_legacy_tables(
        self,
        *,
        since: str,
        prune_normalized: bool = False,
        vacuum: bool = False,
    ) -> int:
        """Record rotation cleanup inputs."""

        self.cleanups.append((since, prune_normalized, vacuum))
        return 3

    def database_storage_metrics(self) -> DatabaseStorageMetrics:
        """Return deterministic storage measurements."""

        return DatabaseStorageMetrics(
            pages=DatabasePageMetrics(
                physical_bytes=self.physical_bytes,
                logical_bytes=96,
                page_size=16,
                page_count=7,
                freelist_pages=1,
            ),
            package_rows=3,
            version_rows=7,
            objects=(DatabaseObjectBytes("table", "versions", 1, 64),),
            package_rows_by_date=(DatabaseDateRows("2026-07-05", 3),),
            version_rows_by_date=(DatabaseDateRows("2026-07-05", 7),),
        )

    def database_write_counts(self) -> DatabaseWriteCounts:
        """Return deterministic process write counts."""

        return DatabaseWriteCounts(2, 5)

    def record_database_metric_sample(self, sample: DatabaseMetricSample) -> None:
        """Record one daily sample."""

        self.samples.append(sample)

    def record_database_rotation(self, event: DatabaseRotationEvent) -> None:
        """Record one completed rotation."""

        self.rotations.append(event)

    def database_rotations_for_release(
        self,
        release_tag: str,
    ) -> tuple[DatabaseRotationEvent, ...]:
        """Return events belonging to one release."""

        return tuple(
            event for event in self.rotations if event.release_tag == release_tag
        )


@dataclass
class _Snapshots:
    fail_prepare: bool = False
    calls: list[str] = field(default_factory=list[str])

    def checkpoint_database(self) -> None:
        """Record the explicit pre-rotation checkpoint."""

        self.calls.append("checkpoint")

    def rotate_database_if_needed(
        self,
        prune_database: Callable[[], object],
        *,
        threshold_bytes: int,
        rotation_stamp: str,
    ) -> SnapshotRotationResult:
        """Run cleanup and record the rotation settings."""

        self.calls.append(f"rotate:{threshold_bytes}:{rotation_stamp}")
        prune_database()
        return SnapshotRotationResult(True, Path("archive.zst"), 200, 75)

    def prepare_database_snapshot(self) -> Path:
        """Return a snapshot path or simulate a publication failure."""

        self.calls.append("prepare")
        if self.fail_prepare:
            raise SnapshotError("snapshot copy failed")
        return Path("index.db")


@dataclass
class _Publisher:
    requests: list[RunPublicationRequest] = field(
        default_factory=list[RunPublicationRequest]
    )

    def publish(self, request: RunPublicationRequest) -> PackageInventory:
        """Record the final publication request."""

        self.requests.append(request)
        return PackageInventory(owners=1, repositories=2, packages=3)


def _request(
    tmp_path: Path,
    *,
    prepare_snapshot: bool,
) -> RunFinalizationRequest:
    return RunFinalizationRequest(
        publication=RunPublicationRequest(
            paths=RunPublicationPaths(tmp_path, tmp_path / "index", tmp_path),
            identity=RunPublicationIdentity("owner", "repo", "master"),
            today="2026-07-05",
        ),
        optout_file=tmp_path / "optout.txt",
        batch_first_started="2026-06-12",
        prepare_snapshot=prepare_snapshot,
        rotation_threshold_bytes=100,
    )


def test_finalization_rotates_prepares_and_then_publishes(tmp_path: Path) -> None:
    """Snapshot modes publish summaries only after a durable archive exists."""

    (tmp_path / "optout.txt").write_text("one/repo/pkg\ntwo/repo/pkg\n")
    state = StateStore(tmp_path / "state.env")
    repository = _Repository()
    snapshots = _Snapshots()
    publisher = _Publisher()
    messages: list[str] = []

    result = RunFinalizationService(
        RunFinalizationServices(
            repository,
            repository,
            repository,
            snapshots,
            publisher,
            state,
        ),
        RunFinalizationExecution(
            lambda: None,
            messages.append,
            lambda: datetime(2026, 7, 5, 12, 34, 56, 789, tzinfo=UTC),
        ),
    ).finalize(_request(tmp_path, prepare_snapshot=True))

    assert result.rotated
    assert result.snapshot == Path("index.db")
    assert result.inventory == PackageInventory(1, 2, 3)
    assert state.get("BKG_OUT") == "2"
    assert snapshots.calls == [
        "checkpoint",
        "rotate:100:2026.07.05T12.34.56.000789Z",
        "prepare",
    ]
    assert repository.cleanups == [("2026-06-12", True, True)]
    assert len(repository.samples) == 1
    assert repository.samples[0].writes == DatabaseWriteCounts(2, 5)
    assert repository.samples[0].rotation_count == 1
    assert repository.rotations == [
        DatabaseRotationEvent(
            release_tag="v2026.7.0",
            rotated_at="2026-07-05T12:34:56.000789Z",
            archive_name="archive.zst",
            source_bytes=200,
            compressed_bytes=75,
            retained_since="2026-06-12",
        )
    ]
    assert publisher.requests[0].rotation_events == tuple(repository.rotations)
    assert messages[:3] == [
        "Preparing the database snapshot...",
        "Rotating the database...",
        "Rotated the database",
    ]
    assert messages[3].startswith("Database finalization telemetry: ")
    assert messages[4].startswith("Database object telemetry: ")
    assert messages[5].startswith("Database date telemetry: ")
    assert messages[6:] == [
        "Prepared the database snapshot",
        "Hydrating templates and cleaning up...",
        "Done!",
    ]


def test_finalization_can_publish_without_snapshot_work(tmp_path: Path) -> None:
    """Mode 2 preserves release rotation history without snapshot work."""

    state = StateStore(tmp_path / "state.env")
    snapshots = _Snapshots()
    publisher = _Publisher()
    event = DatabaseRotationEvent(
        "v2026.7.0",
        "2026-07-01T00:00:00.000000Z",
        "archive.zst",
        200,
        75,
        "2026-06-12",
    )
    repository = _Repository(rotations=[event])

    result = RunFinalizationService(
        RunFinalizationServices(
            repository,
            repository,
            repository,
            snapshots,
            publisher,
            state,
        ),
        RunFinalizationExecution(lambda: None, lambda _message: None),
    ).finalize(_request(tmp_path, prepare_snapshot=False))

    assert not result.rotated
    assert result.snapshot is None
    assert not snapshots.calls
    assert state.get("BKG_OUT") is None
    assert publisher.requests[0].rotation_events == (event,)


def test_finalization_does_not_publish_after_snapshot_failure(tmp_path: Path) -> None:
    """A failed archive copy prevents stale release publication."""

    publisher = _Publisher()
    repository = _Repository(physical_bytes=1)
    service = RunFinalizationService(
        RunFinalizationServices(
            repository,
            repository,
            repository,
            _Snapshots(fail_prepare=True),
            publisher,
            StateStore(tmp_path / "state.env"),
        ),
        RunFinalizationExecution(lambda: None, lambda _message: None),
    )

    with pytest.raises(SnapshotError, match="snapshot copy failed"):
        service.finalize(_request(tmp_path, prepare_snapshot=True))

    assert not publisher.requests
