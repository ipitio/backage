"""Concurrent top-level owner updates with durable outcome effects."""

from __future__ import annotations

import re
import secrets
import shutil
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from threading import Lock
from typing import Literal, Protocol

from ..concurrency import (
    BoundedRunResult,
    BoundedWorkerRunner,
    ConcurrencySettings,
    TaskFailure,
    TaskInterruption,
    WorkerEvent,
)
from ..database.owner_queue import (
    OwnerQueueCompletion,
    OwnerQueueEntry,
    OwnerQueueOutcome,
)
from ..files import atomic_text_output
from ..result import ExitStatus
from ..runtime import GracefulStop, peak_resident_memory_mib
from ..runtime_names import legacy_owner_page_key, legacy_owner_scan_key
from ..state import StateStore
from .lifecycle import OwnerLifecycleResult
from .operations import OwnerUpdateRequest

MessageSink = Callable[[str], None]
OwnerMaterializer = Callable[[tuple[str, ...]], None]
OwnerBatchItemOutcome = Literal[
    "updated",
    "paused",
    "missing",
    "deferred",
    "opted-out",
]
OwnerUpdater = Callable[[OwnerUpdateRequest], OwnerLifecycleResult]
OwnerUpdaterFactory = Callable[[ConcurrencySettings], OwnerUpdater]
_OWNER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}")
_OWNER_MATERIALIZATION_WAVE_SIZE = 100


class OwnerRetirementRepository(Protocol):  # pylint: disable=too-few-public-methods
    """Package retirement needed by outer owner effects."""

    def retire_owner(self, owner: str) -> int:
        """Remove one owner's persisted package state."""

        raise NotImplementedError


class OwnerQueueClaimRepository(Protocol):
    """Durable owner claim operations needed by outer owner execution."""

    def claim_owner_queue_wave(
        self,
        generation: str,
        limit: int,
        claim_token: str,
        now: int,
    ) -> tuple[OwnerQueueEntry, ...]:
        """Claim the next bounded ready wave."""

        raise NotImplementedError

    def finish_owner_queue_claim(self, completion: OwnerQueueCompletion) -> None:
        """Persist one parent-applied worker outcome."""

        raise NotImplementedError


@dataclass(frozen=True)
class QueuedOwner:
    """A validated queued owner identity."""

    owner_id: str
    owner: str

    @property
    def ref(self) -> str:
        """Return the persisted queue representation."""

        return f"{self.owner_id}/{self.owner}"


@dataclass(frozen=True)
class OwnerBatchRequest:
    """Run context shared by every queued owner."""

    since: str
    batch_marker: str
    today: str
    fast_out: bool = False


@dataclass(frozen=True)
class OwnerBatchItem:
    """Completed outcome for one queued owner."""

    owner: QueuedOwner
    outcome: OwnerBatchItemOutcome


@dataclass
class OwnerBatchEffects:
    """Apply owner outcomes to durable database, source, and generated files."""

    retirement: OwnerRetirementRepository
    owner_queue: OwnerQueueClaimRepository
    state: StateStore
    owners_file: Path
    index_dir: Path
    progress: MessageSink
    _lock: Lock = field(default_factory=Lock)

    def apply_opt_out(self, owner: QueuedOwner) -> OwnerBatchItem:
        """Retire an explicitly opted-out owner without consuming its source entry."""

        with self._lock:
            self.progress(f"{owner.owner} was opted out!")
            self._retire(owner, remove_manual=False, announce=False)
        return OwnerBatchItem(owner, "opted-out")

    def apply_result(
        self,
        owner: QueuedOwner,
        result: OwnerLifecycleResult,
    ) -> OwnerBatchItem:
        """Apply source and generated-file effects for a lifecycle outcome."""

        with self._lock:
            pages = result.scan.pages if result.scan is not None else None
            if pages is not None and pages.first_page_empty:
                self._remove_manual_owner(owner.owner)

            if result.outcome == "missing":
                self._retire(owner, remove_manual=True, announce=True)
            elif result.outcome == "paused":
                next_page = pages.next_page if pages is not None else 0
                self.progress(f"Paused {owner.owner} owner scan at page {next_page}")
            elif result.outcome == "updated":
                self._remove_manual_owner(owner.owner)
                self.progress(f"Updated {owner.owner}")
            elif result.outcome != "deferred":
                raise ValueError(
                    f"unknown owner update outcome for {owner.owner}: {result.outcome}"
                )
        return OwnerBatchItem(owner, result.outcome)

    def retire_unavailable(self, owner: str) -> None:
        """Retire one authoritatively missing owner discovered before queueing."""

        if _OWNER_PATTERN.fullmatch(owner) is None:
            raise ValueError(f"invalid owner name for retirement: {owner}")
        with self._lock:
            self._retire_storage(owner)
            self._remove_manual_owner(owner)
            self.progress(f"Retired unavailable owner {owner}")

    def _retire(
        self,
        owner: QueuedOwner,
        *,
        remove_manual: bool,
        announce: bool,
    ) -> None:
        self._retire_storage(owner.owner)
        self.state.delete_matching(
            keys=(
                legacy_owner_scan_key(owner.owner_id),
                legacy_owner_page_key(owner.owner_id),
            )
        )
        if remove_manual:
            self._remove_manual_owner(owner.owner)
        if announce:
            self.progress(f"Retired unavailable owner {owner.owner}")

    def _retire_storage(self, owner: str) -> None:
        self.retirement.retire_owner(owner)
        owner_dir = self.index_dir / owner
        if owner_dir.exists():
            shutil.rmtree(owner_dir)

    def _remove_manual_owner(self, owner: str) -> None:
        lines = self.owners_file.read_text(encoding="utf-8").splitlines()
        retained = [line for line in lines if line.rsplit("/", maxsplit=1)[-1] != owner]
        if retained == lines:
            return
        with atomic_text_output(self.owners_file) as output:
            if retained:
                output.write("\n".join(retained))
                output.write("\n")


@dataclass(frozen=True)
class OwnerBatchExecution:  # pylint: disable=too-many-instance-attributes
    """Concurrency, paths, and runtime callbacks for a queued-owner batch."""

    optout_file: Path
    concurrency: ConcurrencySettings
    check_stop: Callable[[], None]
    progress: MessageSink
    diagnostic: MessageSink
    materialize: OwnerMaterializer = lambda _owners: None
    now: Callable[[], int] = lambda: int(time.time())
    token: Callable[[], str] = lambda: secrets.token_hex(16)


@dataclass(frozen=True)
class _OwnerWaveUpdate:
    request: OwnerBatchRequest
    updater: OwnerUpdater
    opted_out: set[str]
    workers: int


@dataclass(frozen=True)
class _OwnerWorkerResult:
    owner: QueuedOwner
    result: OwnerLifecycleResult | None = None
    opted_out: bool = False


class OwnerBatchService:  # pylint: disable=too-few-public-methods
    """Run all queued owners through one shared-process worker pool."""

    def __init__(
        self,
        updater_factory: OwnerUpdaterFactory,
        effects: OwnerBatchEffects,
        execution: OwnerBatchExecution,
        *,
        materialization_wave_size: int = _OWNER_MATERIALIZATION_WAVE_SIZE,
    ) -> None:
        if materialization_wave_size < 1:
            raise ValueError("materialization wave size must be positive")
        self.updater_factory = updater_factory
        self.effects = effects
        self.execution = execution
        self.materialization_wave_size = materialization_wave_size

    def run(self, request: OwnerBatchRequest) -> ExitStatus:
        """Run queued owners and preserve completed effects across graceful stops."""

        claim_token = self.execution.token()
        opted_out = _owner_opt_outs(self.execution.optout_file)
        wave_number = 1
        while True:
            claim_started_at = time.monotonic()
            claimed = self.effects.owner_queue.claim_owner_queue_wave(
                request.batch_marker,
                self.materialization_wave_size,
                claim_token,
                self.execution.now(),
            )
            claim_elapsed = max(0.0, time.monotonic() - claim_started_at)
            self.execution.progress(
                "Owner queue claim: "
                f"wave={wave_number} claimed={len(claimed)} "
                f"sqlite={claim_elapsed:.3f}s; "
                f"peak_rss={peak_resident_memory_mib():.1f}MiB"
            )
            if not claimed:
                return ExitStatus.SUCCESS
            wave = tuple(QueuedOwner(entry.owner_id, entry.owner) for entry in claimed)
            owner_workers, per_owner_workers = allocate_owner_worker_counts(
                len(wave),
                self.execution.concurrency.max_workers,
            )
            update = _OwnerWaveUpdate(
                request,
                self.updater_factory(
                    replace(
                        self.execution.concurrency,
                        max_workers=per_owner_workers,
                    )
                ),
                opted_out,
                owner_workers,
            )
            if not self._materialize_wave(wave, wave_number):
                return ExitStatus.GRACEFUL_STOP
            status, items = self._run_wave(wave, update, wave_number)
            self._apply_items(items, claim_token, request.batch_marker)
            if status is not ExitStatus.SUCCESS:
                return status
            wave_number += 1

    def _materialize_wave(
        self,
        wave: tuple[QueuedOwner, ...],
        wave_number: int,
    ) -> bool:
        owner_names = tuple(owner.owner for owner in wave)
        self.execution.progress(
            f"Materializing owner wave {wave_number} ({len(wave)} tree(s))..."
        )
        started_at = time.monotonic()
        try:
            self.execution.check_stop()
            self.execution.materialize(owner_names)
        except GracefulStop as error:
            self.execution.diagnostic(f"Graceful stop requested: {error}")
            return False
        elapsed = max(0.0, time.monotonic() - started_at)
        self.execution.progress(
            f"Materialized owner wave {wave_number} in {elapsed:.1f}s"
        )
        return True

    def _run_wave(
        self,
        wave: tuple[QueuedOwner, ...],
        update: _OwnerWaveUpdate,
        wave_number: int,
    ) -> tuple[ExitStatus, tuple[_OwnerWorkerResult, ...]]:
        runner = BoundedWorkerRunner(
            replace(self.execution.concurrency, max_workers=update.workers),
            check_stop=self.execution.check_stop,
            event_sink=self._worker_event,
        )
        result = runner.run(
            wave,
            lambda owner: self._update_one(
                owner,
                update.request,
                update.updater,
                update.opted_out,
            ),
            task_name=lambda owner: owner.owner,
        )
        self._report_worker_metrics(wave_number, result)
        self._report_failures(result.failures, result.interrupted)
        items = tuple(
            completed.value
            for completed in sorted(result.completed, key=lambda item: item.index)
        )
        if result.stopped:
            return ExitStatus.GRACEFUL_STOP, items
        if not result.ok:
            return ExitStatus.NON_FATAL, items
        return ExitStatus.SUCCESS, items

    def _report_worker_metrics(
        self,
        wave_number: int,
        result: BoundedRunResult[_OwnerWorkerResult],
    ) -> None:
        metrics = result.metrics
        if metrics is None:
            return
        stopped_tasks = sum(
            interruption.reason == "graceful-stop"
            for interruption in result.interrupted
        )
        other_interruptions = len(result.interrupted) - stopped_tasks
        self.execution.progress(
            "Owner worker telemetry: "
            f"wave={wave_number} requested={metrics.counts.requested_tasks} "
            f"submitted={metrics.counts.submitted_tasks} "
            f"completed={len(result.completed)} failed={len(result.failures)} "
            f"stopped={stopped_tasks} interrupted={other_interruptions} "
            f"workers={metrics.counts.max_workers} "
            f"peak={metrics.counts.peak_in_flight}; "
            f"wall={metrics.timing.wall_seconds:.3f}s "
            f"process_cpu={metrics.timing.process_cpu_seconds:.3f}s "
            f"process_cpu_cores={metrics.process_cpu_cores:.2f} "
            f"worker_occupancy={metrics.worker_occupancy:.1%} "
            f"queue_wait_p95={metrics.tasks.queue_wait_p95_seconds:.3f}s "
            f"queue_wait_max={metrics.tasks.queue_wait_max_seconds:.3f}s "
            f"task_p50={metrics.tasks.task_p50_seconds:.3f}s "
            f"task_p95={metrics.tasks.task_p95_seconds:.3f}s "
            f"task_max={metrics.tasks.task_max_seconds:.3f}s "
            f"slowest={metrics.tasks.slowest_task or '-'} "
            f"drain={metrics.timing.drain_seconds:.3f}s"
        )

    def _update_one(
        self,
        owner: QueuedOwner,
        request: OwnerBatchRequest,
        updater: OwnerUpdater,
        opted_out: set[str],
    ) -> _OwnerWorkerResult:
        if owner.owner in opted_out:
            return _OwnerWorkerResult(owner, opted_out=True)
        self.execution.progress(f"Updating {owner.owner}...")
        return _OwnerWorkerResult(
            owner,
            updater(
                OwnerUpdateRequest(
                    owner_id=owner.owner_id,
                    owner=owner.owner,
                    since=request.since,
                    batch_marker=request.batch_marker,
                    today=request.today,
                    fast_out=request.fast_out,
                )
            ),
        )

    def _apply_items(
        self,
        items: tuple[_OwnerWorkerResult, ...],
        claim_token: str,
        generation: str,
    ) -> None:
        for completed in items:
            item = (
                self.effects.apply_opt_out(completed.owner)
                if completed.opted_out
                else self.effects.apply_result(
                    completed.owner,
                    _required_result(completed),
                )
            )
            self.effects.owner_queue.finish_owner_queue_claim(
                OwnerQueueCompletion(
                    generation=generation,
                    owner_id=item.owner.owner_id,
                    claim_token=claim_token,
                    outcome=_queue_outcome(item.outcome),
                    finished_at=self.execution.now(),
                )
            )

    def _worker_event(self, event: WorkerEvent) -> None:
        if event.kind == "stop-requested":
            reason = event.message or "requested"
            self.execution.diagnostic(f"Graceful stop requested: {reason}")
            self.execution.progress("Waiting for active owner updates to stop...")
        elif event.kind == "drain-timeout":
            self.execution.diagnostic(
                f"Graceful stop window exceeded for active owner {event.name}"
            )

    def _report_failures(
        self,
        failures: Sequence[TaskFailure],
        interruptions: Sequence[TaskInterruption],
    ) -> None:
        for failure in failures:
            self.execution.diagnostic(
                f"Owner update failed for {failure.name}: {failure.error}"
            )
        for interruption in interruptions:
            if interruption.reason == "graceful-stop":
                continue
            self.execution.diagnostic(
                f"Owner update interrupted for {interruption.name}: "
                f"{interruption.reason}"
            )


def parse_owner_queue(values: Sequence[str]) -> tuple[QueuedOwner, ...]:
    """Parse and deduplicate persisted ID/login queue entries."""

    owners: list[QueuedOwner] = []
    seen: set[str] = set()
    for value in values:
        owner_id, separator, owner = value.strip().partition("/")
        if (
            not separator
            or not owner_id.isdecimal()
            or owner_id.startswith("0")
            or _OWNER_PATTERN.fullmatch(owner) is None
        ):
            raise ValueError(f"invalid queued owner reference: {value!r}")
        key = owner.casefold()
        if key not in seen:
            seen.add(key)
            owners.append(QueuedOwner(owner_id, owner))
    return tuple(owners)


def allocate_owner_worker_counts(
    owner_count: int, total_workers: int
) -> tuple[int, int]:
    """Divide one worker budget between owners and each owner's package work."""

    if owner_count <= 0 or total_workers <= 0:
        raise ValueError("owner and worker counts must be positive")
    owner_workers = min(owner_count, max(1, total_workers // 2))
    return owner_workers, max(1, total_workers // owner_workers)


def _owner_opt_outs(path: Path) -> set[str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return set()
    return {line.strip() for line in lines if _OWNER_PATTERN.fullmatch(line.strip())}


def _required_result(completed: _OwnerWorkerResult) -> OwnerLifecycleResult:
    if completed.result is None:
        raise ValueError(f"owner update for {completed.owner.owner} has no result")
    return completed.result


def _queue_outcome(outcome: OwnerBatchItemOutcome) -> OwnerQueueOutcome:
    return outcome
