"""Concurrent top-level owner updates with durable outcome effects."""

from __future__ import annotations

import re
import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from threading import Lock
from typing import Literal, Protocol

from .concurrency import (
    BoundedWorkerRunner,
    ConcurrencySettings,
    TaskFailure,
    TaskInterruption,
    WorkerEvent,
)
from .files import atomic_text_output
from .owner_lifecycle import OwnerLifecycleResult
from .owner_operations import OwnerUpdateRequest
from .result import ExitStatus
from .runtime import GracefulStop
from .state import StateStore

MessageSink = Callable[[str], None]
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


class OwnerRetirementRepository(Protocol):  # pylint: disable=too-few-public-methods
    """Database operation needed by outer owner effects."""

    def retire_owner(self, owner: str) -> int:
        """Remove one owner's persisted package state."""

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
    fast_out: bool = False


@dataclass(frozen=True)
class OwnerBatchItem:
    """Completed outcome for one queued owner."""

    owner: QueuedOwner
    outcome: OwnerBatchItemOutcome


@dataclass
class OwnerBatchEffects:
    """Apply owner outcomes to durable database, source, and generated files."""

    repository: OwnerRetirementRepository
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

    def _retire(
        self,
        owner: QueuedOwner,
        *,
        remove_manual: bool,
        announce: bool,
    ) -> None:
        self.repository.retire_owner(owner.owner)
        self.state.delete_matching(
            keys=(
                f"BKG_OWNER_SCAN_{owner.owner_id}",
                f"BKG_PAGE_{owner.owner_id}",
            )
        )
        owner_dir = self.index_dir / owner.owner
        if owner_dir.exists():
            shutil.rmtree(owner_dir)
        if remove_manual:
            self._remove_manual_owner(owner.owner)
        if announce:
            self.progress(f"Retired unavailable owner {owner.owner}")

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
class OwnerBatchExecution:
    """Concurrency, paths, and runtime callbacks for a queued-owner batch."""

    state: StateStore
    optout_file: Path
    concurrency: ConcurrencySettings
    check_stop: Callable[[], None]
    progress: MessageSink
    diagnostic: MessageSink


class OwnerBatchService:  # pylint: disable=too-few-public-methods
    """Run all queued owners through one shared-process worker pool."""

    def __init__(
        self,
        updater_factory: OwnerUpdaterFactory,
        effects: OwnerBatchEffects,
        execution: OwnerBatchExecution,
    ) -> None:
        self.updater_factory = updater_factory
        self.effects = effects
        self.execution = execution

    def run(self, request: OwnerBatchRequest) -> ExitStatus:
        """Run queued owners and preserve completed effects across graceful stops."""

        owners = parse_owner_queue(self.execution.state.get_set("BKG_OWNERS_QUEUE"))
        if not owners:
            return ExitStatus.SUCCESS
        owner_workers, per_owner_workers = allocate_owner_worker_counts(
            len(owners),
            self.execution.concurrency.max_workers,
        )
        updater = self.updater_factory(
            replace(self.execution.concurrency, max_workers=per_owner_workers)
        )
        opted_out = _owner_opt_outs(self.execution.optout_file)
        runner = BoundedWorkerRunner(
            replace(self.execution.concurrency, max_workers=owner_workers),
            check_stop=self.execution.check_stop,
            event_sink=self._worker_event,
        )
        result = runner.run(
            owners,
            lambda owner: self._update_one(owner, request, updater, opted_out),
            task_name=lambda owner: owner.owner,
        )
        self._report_failures(result.failures, result.interrupted)
        if result.stopped:
            return ExitStatus.GRACEFUL_STOP
        if not result.ok:
            return ExitStatus.NON_FATAL
        return ExitStatus.SUCCESS

    def _update_one(
        self,
        owner: QueuedOwner,
        request: OwnerBatchRequest,
        updater: OwnerUpdater,
        opted_out: set[str],
    ) -> OwnerBatchItem:
        if owner.owner in opted_out:
            return self.effects.apply_opt_out(owner)
        self.execution.progress(f"Updating {owner.owner}...")
        result = updater(
            OwnerUpdateRequest(
                owner.owner_id,
                owner.owner,
                request.since,
                request.batch_marker,
                request.fast_out,
            )
        )
        return self.effects.apply_result(owner, result)

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
            if isinstance(failure.error, GracefulStop):
                continue
            self.execution.diagnostic(
                f"Owner update failed for {failure.name}: {failure.error}"
            )
        for interruption in interruptions:
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
