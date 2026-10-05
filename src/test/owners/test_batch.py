"""Tests for shared-process queued owner updates and durable effects."""

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from threading import Barrier, Event

import pytest

from bkg_py.concurrency import ConcurrencySettings
from bkg_py.database.composition import DatabaseRepositories
from bkg_py.database.settings import DatabaseSettings
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
from bkg_py.publication.promotion import PublicationRecoveryError
from bkg_py.result import ExitStatus
from bkg_py.runtime import GracefulStop, StopController
from bkg_py.state import StateStore
from bkg_py.workspace import GitIndexRepository


@dataclass
class _Repository:
    database: DatabaseRepositories
    retired: list[str] = field(default_factory=list[str])

    def retire_owner(self, owner: str) -> int:
        """Record one owner retirement."""

        self.retired.append(owner)
        return self.database.packages.retire_owner(owner)


@dataclass
class _Messages:
    progress: list[str] = field(default_factory=list[str])
    diagnostic: list[str] = field(default_factory=list[str])
    allocated: list[int] = field(default_factory=list[int])
    materialized: list[tuple[str, ...]] = field(default_factory=list[tuple[str, ...]])


@dataclass
class _Harness:
    service: OwnerBatchService
    repository: _Repository
    messages: _Messages
    stop: StopController


def _service(  # pylint: disable=too-many-locals
    tmp_path: Path,
    updater: Callable[[OwnerUpdateRequest], OwnerLifecycleResult],
    *,
    queued: tuple[str, ...] = ("1/alpha",),
    optout: tuple[str, ...] = (),
    materialization_wave_size: int = 100,
) -> _Harness:
    state = StateStore(tmp_path / "state.env")
    stop = StopController(state, max_duration=-1)
    database = DatabaseRepositories(
        DatabaseSettings(tmp_path / "index.db"), check_stop=stop.check
    )
    database.owner_queue.prepare_owner_queue("batch-1", queued, 1)
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
    repository = _Repository(database)
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
            database.owner_queue,
            state,
            owners_file,
            GitIndexRepository(index_dir).remove_owner_tree,
            messages.progress.append,
        ),
        OwnerBatchExecution(
            optout_file,
            ConcurrencySettings(4),
            stop.check,
            messages.progress.append,
            messages.diagnostic.append,
            finalization_scope=stop.finalization_scope,
            materialize=messages.materialized.append,
            now=lambda: 2,
            token=lambda: "test-claim",
        ),
        materialization_wave_size=materialization_wave_size,
    )
    return _Harness(service, repository, messages, stop)


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

    status = harness.service.run(
        OwnerBatchRequest("2026-07-01", "batch-1", "2026-07-02")
    )

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
    remaining = harness.repository.database.owner_queue.owner_queue_entries("batch-1")
    assert tuple(entry.ref for entry in remaining) == ("3/paused",)
    completed = harness.repository.database.owner_queue.owner_queue_entries(
        "batch-1",
        status="completed",
    )
    assert {entry.owner for entry in completed} == {"alpha", "deferred"}
    assert harness.messages.allocated == [2]
    assert "Updated alpha" in harness.messages.progress
    assert "Retired unavailable owner missing" in harness.messages.progress
    assert any(
        message.startswith("Owner queue claim: wave=1 claimed=5 sqlite=")
        for message in harness.messages.progress
    )
    assert any(
        message.startswith(
            "Owner worker telemetry: wave=1 requested=5 submitted=5 completed=5"
        )
        for message in harness.messages.progress
    )
    assert not harness.messages.diagnostic


def test_owner_batch_materializes_bounded_waves(tmp_path: Path) -> None:
    """Only a small runway of owner trees is hydrated before each worker wave."""

    queued = tuple(f"{index}/{name}" for index, name in enumerate("abcdefghij", 1))

    harness = _service(
        tmp_path,
        lambda _request: OwnerLifecycleResult("updated"),
        queued=queued,
        materialization_wave_size=4,
    )

    status = harness.service.run(
        OwnerBatchRequest("2026-07-01", "batch-1", "2026-07-02")
    )

    assert status == ExitStatus.SUCCESS
    assert harness.messages.materialized == [
        ("a", "b", "c", "d"),
        ("e", "f", "g", "h"),
        ("i", "j"),
    ]


def test_owner_batch_does_not_materialize_a_later_wave_after_stop(
    tmp_path: Path,
) -> None:
    """A graceful stop leaves every not-yet-started wave unhydrated."""

    queued = tuple(f"{index}/{name}" for index, name in enumerate("abcdefgh", 1))

    def stop_on_first(request: OwnerUpdateRequest) -> OwnerLifecycleResult:
        if request.owner == "a":
            raise GracefulStop("elapsed")
        return OwnerLifecycleResult("updated")

    harness = _service(
        tmp_path,
        stop_on_first,
        queued=queued,
        materialization_wave_size=4,
    )

    status = harness.service.run(
        OwnerBatchRequest("2026-07-01", "batch-1", "2026-07-02")
    )

    assert status == ExitStatus.GRACEFUL_STOP
    assert harness.messages.materialized == [("a", "b", "c", "d")]
    assert any(
        "failed=0 stopped=1 interrupted=0" in message
        for message in harness.messages.progress
    )


def test_completed_owner_claims_survive_a_real_graceful_stop(tmp_path: Path) -> None:
    """A stopped wave checkpoints completed owners before the next run recovers it."""

    completed = Event()

    def update(request: OwnerUpdateRequest) -> OwnerLifecycleResult:
        if request.owner == "alpha":
            completed.set()
            return OwnerLifecycleResult("updated")
        assert completed.wait(timeout=5)
        harness.stop.request_stop("elapsed")
        harness.stop.check()
        raise AssertionError("stop check must interrupt the unfinished owner")

    harness = _service(
        tmp_path,
        update,
        queued=("1/alpha", "2/unfinished"),
    )

    status = harness.service.run(
        OwnerBatchRequest("2026-07-01", "batch-1", "2026-07-02")
    )

    assert status == ExitStatus.GRACEFUL_STOP
    with pytest.raises(GracefulStop, match="elapsed"):
        harness.stop.check()
    resumed = DatabaseRepositories(DatabaseSettings(tmp_path / "index.db"))
    recovered = resumed.owner_queue.prepare_owner_queue("batch-1", (), 3)
    assert tuple(entry.ref for entry in recovered) == ("2/unfinished",)
    assert (tmp_path / "owners.txt").read_text(encoding="utf-8") == "unfinished\n"
    assert not any(
        message.startswith("Owner update failed")
        for message in harness.messages.diagnostic
    )


def test_paused_scan_keeps_manual_owner_until_completion(tmp_path: Path) -> None:
    """An incomplete owner scan cannot consume its manual source entry."""

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

    status = harness.service.run(
        OwnerBatchRequest("2026-07-01", "batch-1", "2026-07-02")
    )

    assert status == ExitStatus.SUCCESS
    assert (tmp_path / "owners.txt").read_text(encoding="utf-8") == "alpha\n"


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

    status = harness.service.run(
        OwnerBatchRequest("2026-07-01", "batch-1", "2026-07-02")
    )

    assert status == expected_status
    assert any(
        diagnostic_fragment in message for message in harness.messages.diagnostic
    )


@pytest.mark.parametrize("concurrent_stop", [False, True])
def test_owner_batch_aborts_after_failed_restoration(
    tmp_path: Path, concurrent_stop: bool
) -> None:
    """A concurrent stop must not turn an unrestored endpoint into a success."""

    queued = ("1/alpha", "2/beta") if concurrent_stop else ("1/alpha",)
    barrier = Barrier(len(queued))

    def update(request: OwnerUpdateRequest) -> OwnerLifecycleResult:
        barrier.wait(timeout=5)
        if request.owner == "beta":
            raise GracefulStop("concurrent stop")
        raise PublicationRecoveryError("retained outputs")

    harness = _service(tmp_path, update, queued=queued)

    status = harness.service.run(
        OwnerBatchRequest("2026-07-01", "batch-1", "2026-07-02")
    )

    assert status == ExitStatus.NON_FATAL
    assert any("retained outputs" in message for message in harness.messages.diagnostic)


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
