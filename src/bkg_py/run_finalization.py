"""Finalize database snapshots and generated run summaries in one operation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

from .database import PackageInventory
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


class SnapshotFinalizer(Protocol):
    """Snapshot operations required by run finalization."""

    def checkpoint_database(self) -> None:
        """Checkpoint the live database."""

        raise NotImplementedError

    def database_size(self) -> int:
        """Return the checkpointed live database size."""

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
            if (
                self.services.snapshots.database_size()
                >= request.rotation_threshold_bytes
            ):
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
                self.execution.progress("Rotated the database")
            snapshot = self.services.snapshots.prepare_database_snapshot()
            self.execution.progress("Prepared the database snapshot")

        self.execution.progress("Hydrating templates and cleaning up...")
        inventory = self.services.publisher.publish(
            replace(request.publication, rotated=rotated)
        )
        self.execution.progress("Done!")
        return RunFinalizationResult(rotated, snapshot, inventory)


def _line_count(path: Path) -> int:
    try:
        return len(path.read_text(encoding="utf-8").splitlines())
    except FileNotFoundError:
        return 0
