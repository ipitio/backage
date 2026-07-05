"""Tests for combined snapshot and run-summary finalization."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from bkg_py.database_models import PackageInventory
from bkg_py.run_finalization import (
    RunFinalizationExecution,
    RunFinalizationRequest,
    RunFinalizationService,
    RunFinalizationServices,
)
from bkg_py.run_publication import (
    RunPublicationIdentity,
    RunPublicationPaths,
    RunPublicationRequest,
)
from bkg_py.snapshots import SnapshotError, SnapshotRotationResult
from bkg_py.state import StateStore


@dataclass
class _Repository:
    cleanups: list[tuple[str, bool, bool]] = field(
        default_factory=list[tuple[str, bool, bool]]
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


@dataclass
class _Snapshots:
    size: int
    fail_prepare: bool = False
    calls: list[str] = field(default_factory=list[str])

    def checkpoint_database(self) -> None:
        """Record the explicit pre-rotation checkpoint."""

        self.calls.append("checkpoint")

    def database_size(self) -> int:
        """Return the configured live database size."""

        self.calls.append("size")
        return self.size

    def rotate_database_if_needed(
        self,
        prune_database: Callable[[], object],
        *,
        threshold_bytes: int,
        date_stamp: str,
    ) -> SnapshotRotationResult:
        """Run cleanup and record the rotation settings."""

        self.calls.append(f"rotate:{threshold_bytes}:{date_stamp}")
        prune_database()
        return SnapshotRotationResult(True, Path("archive.zst"))

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
            rotated=False,
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
    snapshots = _Snapshots(size=101)
    publisher = _Publisher()
    messages: list[str] = []

    result = RunFinalizationService(
        RunFinalizationServices(repository, snapshots, publisher, state),
        RunFinalizationExecution(lambda: None, messages.append),
    ).finalize(_request(tmp_path, prepare_snapshot=True))

    assert result.rotated
    assert result.snapshot == Path("index.db")
    assert result.inventory == PackageInventory(1, 2, 3)
    assert state.get("BKG_OUT") == "2"
    assert snapshots.calls == [
        "checkpoint",
        "size",
        "rotate:100:2026.07.05",
        "prepare",
    ]
    assert repository.cleanups == [("2026-06-12", True, True)]
    assert publisher.requests[0].rotated
    assert messages == [
        "Preparing the database snapshot...",
        "Rotating the database...",
        "Rotated the database",
        "Prepared the database snapshot",
        "Hydrating templates and cleaning up...",
        "Done!",
    ]


def test_finalization_can_publish_without_snapshot_work(tmp_path: Path) -> None:
    """Mode 2 retains summary publication without touching snapshot state."""

    state = StateStore(tmp_path / "state.env")
    snapshots = _Snapshots(size=1_000)
    publisher = _Publisher()

    result = RunFinalizationService(
        RunFinalizationServices(_Repository(), snapshots, publisher, state),
        RunFinalizationExecution(lambda: None, lambda _message: None),
    ).finalize(_request(tmp_path, prepare_snapshot=False))

    assert not result.rotated
    assert result.snapshot is None
    assert not snapshots.calls
    assert state.get("BKG_OUT") is None
    assert not publisher.requests[0].rotated


def test_finalization_does_not_publish_after_snapshot_failure(tmp_path: Path) -> None:
    """A failed archive copy prevents stale release publication."""

    publisher = _Publisher()
    service = RunFinalizationService(
        RunFinalizationServices(
            _Repository(),
            _Snapshots(size=1, fail_prepare=True),
            publisher,
            StateStore(tmp_path / "state.env"),
        ),
        RunFinalizationExecution(lambda: None, lambda _message: None),
    )

    with pytest.raises(SnapshotError, match="snapshot copy failed"):
        service.finalize(_request(tmp_path, prepare_snapshot=True))

    assert not publisher.requests
