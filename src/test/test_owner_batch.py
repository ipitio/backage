"""Tests for shared-process queued owner updates and durable effects."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from bkg_py.concurrency import ConcurrencySettings
from bkg_py.owners.batch import (
    OwnerBatchEffects,
    OwnerBatchExecution,
    OwnerBatchRequest,
    OwnerBatchService,
    allocate_owner_worker_counts,
    parse_owner_queue,
)
from bkg_py.owners.lifecycle import OwnerLifecycleResult
from bkg_py.owners.operations import OwnerUpdateRequest
from bkg_py.owners.scan_pages import OwnerScanPagesResult
from bkg_py.owners.updates import OwnerScanOutcome
from bkg_py.result import ExitStatus
from bkg_py.runtime import GracefulStop
from bkg_py.state import StateStore


@dataclass
class _Repository:
    retired: list[str] = field(default_factory=list[str])

    def retire_owner(self, owner: str) -> int:
        """Record one owner retirement."""

        self.retired.append(owner)
        return 1


@dataclass
class _Messages:
    progress: list[str] = field(default_factory=list[str])
    diagnostic: list[str] = field(default_factory=list[str])
    allocated: list[int] = field(default_factory=list[int])


@dataclass
class _Harness:
    service: OwnerBatchService
    repository: _Repository
    messages: _Messages


def _service(
    tmp_path: Path,
    updater: Callable[[OwnerUpdateRequest], OwnerLifecycleResult],
    *,
    queued: tuple[str, ...] = ("1/alpha",),
    optout: tuple[str, ...] = (),
    workers: int = 4,
) -> _Harness:
    state = StateStore(tmp_path / "state.env")
    for owner in queued:
        state.add_to_set("BKG_OWNERS_QUEUE", owner)
    owners_file = tmp_path / "owners.txt"
    owners_file.write_text(
        "".join(f"{owner.split('/', maxsplit=1)[1]}\n" for owner in queued),
        encoding="utf-8",
    )
    optout_file = tmp_path / "optout.txt"
    optout_file.write_text("".join(f"{owner}\n" for owner in optout), encoding="utf-8")
    index_dir = tmp_path / "index"
    for owner in queued:
        (index_dir / owner.split("/", maxsplit=1)[1]).mkdir(parents=True)
    repository = _Repository()
    messages = _Messages()

    def factory(
        settings: ConcurrencySettings,
    ) -> Callable[[OwnerUpdateRequest], OwnerLifecycleResult]:
        messages.allocated.append(settings.max_workers)
        return updater

    service = OwnerBatchService(
        factory,
        OwnerBatchEffects(
            repository,
            state,
            owners_file,
            index_dir,
            messages.progress.append,
        ),
        OwnerBatchExecution(
            state,
            optout_file,
            ConcurrencySettings(workers),
            lambda: None,
            messages.progress.append,
            messages.diagnostic.append,
        ),
    )
    return _Harness(service, repository, messages)


def test_owner_batch_applies_each_completed_outcome(tmp_path: Path) -> None:
    """Completed owner effects persist safely inside the shared worker process."""

    queued = (
        "1/alpha",
        "2/missing",
        "3/paused",
        "4/deferred",
        "5/opted",
    )
    called: list[str] = []

    def update(request: OwnerUpdateRequest) -> OwnerLifecycleResult:
        called.append(request.owner)
        if request.owner == "alpha":
            return OwnerLifecycleResult("updated")
        if request.owner == "missing":
            return OwnerLifecycleResult("missing")
        if request.owner == "paused":
            return OwnerLifecycleResult("paused")
        return OwnerLifecycleResult("deferred")

    harness = _service(
        tmp_path,
        update,
        queued=queued,
        optout=("opted",),
    )
    state = StateStore(tmp_path / "state.env")
    state.set_many(
        {
            "BKG_OWNER_SCAN_2": "missing-scan",
            "BKG_PAGE_2": 4,
            "BKG_OWNER_SCAN_5": "opted-scan",
            "BKG_PAGE_5": 7,
        }
    )

    status = harness.service.run(OwnerBatchRequest("2026-07-01", "batch-1"))

    assert status == ExitStatus.SUCCESS
    assert set(called) == {"alpha", "missing", "paused", "deferred"}
    assert set(harness.repository.retired) == {"missing", "opted"}
    assert (tmp_path / "owners.txt").read_text(encoding="utf-8").splitlines() == [
        "paused",
        "deferred",
        "opted",
    ]
    assert not (tmp_path / "index/missing").exists()
    assert not (tmp_path / "index/opted").exists()
    assert (tmp_path / "index/alpha").is_dir()
    assert state.get("BKG_OWNER_SCAN_2") is None
    assert state.get("BKG_PAGE_2") is None
    assert state.get("BKG_OWNER_SCAN_5") is None
    assert state.get("BKG_PAGE_5") is None
    assert harness.messages.allocated == [2]
    assert "Updated alpha" in harness.messages.progress
    assert "Retired unavailable owner missing" in harness.messages.progress
    assert not harness.messages.diagnostic


def test_first_empty_page_removes_manual_owner_before_pause(tmp_path: Path) -> None:
    """An authoritative empty first page consumes a manual source entry."""

    def update(_request: OwnerUpdateRequest) -> OwnerLifecycleResult:
        return OwnerLifecycleResult(
            "paused",
            scan=OwnerScanOutcome(
                OwnerScanPagesResult(
                    next_page=2,
                    pages_processed=1,
                    first_page_empty=True,
                )
            ),
        )

    harness = _service(tmp_path, update)

    status = harness.service.run(OwnerBatchRequest("2026-07-01", "batch-1"))

    assert status == ExitStatus.SUCCESS
    assert (tmp_path / "owners.txt").read_text(encoding="utf-8") == ""


@pytest.mark.parametrize(
    ("error", "expected_status", "diagnostic_fragment"),
    [
        (GracefulStop("elapsed"), ExitStatus.GRACEFUL_STOP, "Graceful stop"),
        (RuntimeError("broken owner"), ExitStatus.NON_FATAL, "broken owner"),
    ],
)
def test_owner_batch_maps_worker_failures(
    tmp_path: Path,
    error: Exception,
    expected_status: ExitStatus,
    diagnostic_fragment: str,
) -> None:
    """Stops remain resumable while unexpected worker errors abort publication."""

    def update(_request: OwnerUpdateRequest) -> OwnerLifecycleResult:
        raise error

    harness = _service(
        tmp_path,
        update,
    )

    status = harness.service.run(OwnerBatchRequest("2026-07-01", "batch-1"))

    assert status == expected_status
    assert any(
        diagnostic_fragment in message for message in harness.messages.diagnostic
    )


def test_owner_queue_parser_validates_and_deduplicates() -> None:
    """Malformed persisted identities cannot become filesystem paths."""

    owners = parse_owner_queue(("1/Alpha", "2/alpha", "3/beta"))

    assert tuple(owner.ref for owner in owners) == ("1/Alpha", "3/beta")
    with pytest.raises(ValueError, match="invalid queued owner reference"):
        parse_owner_queue(("4/../escape",))


@pytest.mark.parametrize(
    ("owners", "workers", "expected"),
    [
        (1, 8, (1, 8)),
        (2, 8, (2, 4)),
        (20, 8, (4, 2)),
        (20, 1, (1, 1)),
    ],
)
def test_owner_worker_allocation_bounds_nested_concurrency(
    owners: int,
    workers: int,
    expected: tuple[int, int],
) -> None:
    """Large queues share one total budget while a single large owner keeps it."""

    assert allocate_owner_worker_counts(owners, workers) == expected
