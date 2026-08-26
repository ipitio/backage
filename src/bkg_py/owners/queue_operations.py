"""Prepare the durable owner queue after global discovery completes."""

import random
import time
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from ..database.owner.queue import (
    OwnerQueueAdmission,
    OwnerQueueCandidate,
    OwnerQueueEntry,
)
from ..discovery.values import normalize_owner_lines
from ..files import atomic_text_output
from ..runtime import peak_resident_memory_mib
from ..runtime_names import RunFile, StateKey
from ..state import StateStore
from .queue import OwnerQueuePaths, OwnerQueueSelector

MessageSink = Callable[[str], None]
StopCheck = Callable[[], None]
RetireOwner = Callable[[str], None]


class OwnerCandidateResolver(Protocol):  # pylint: disable=too-few-public-methods
    """Identity operation needed by owner queue preparation."""

    def resolve_candidates(
        self,
        candidates: Iterable[str],
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Return canonical owner refs and authoritatively missing logins."""

        raise NotImplementedError


class DurableOwnerQueueRepository(Protocol):
    """Database operations needed by owner queue preparation."""

    def deferred_owners(self, now: int) -> tuple[tuple[str, int], ...]:
        """Return owner names and future retry timestamps."""

        raise NotImplementedError

    def admit_owner_queue(
        self,
        generation: str,
        admissions: tuple[OwnerQueueAdmission, ...],
        now: int,
    ) -> tuple[OwnerQueueEntry, ...]:
        """Add or promote canonical owner work."""

        raise NotImplementedError

    def known_owner_queue_candidates(
        self,
        generation: str,
        candidates: tuple[str, ...],
    ) -> frozenset[str]:
        """Return candidate logins already attempted this generation."""

        raise NotImplementedError

    def record_owner_queue_candidates(
        self,
        generation: str,
        candidates: tuple[OwnerQueueCandidate, ...],
        admissions: tuple[OwnerQueueAdmission, ...],
        now: int,
    ) -> tuple[OwnerQueueEntry, ...]:
        """Persist attempted candidates and canonical admissions together."""

        raise NotImplementedError


@dataclass(frozen=True)
class OwnerQueuePreparationPaths:
    """Files consulted while constructing one owner queue."""

    connections: Path
    manual_owners: Path
    index_directory: Path
    working_directory: Path


@dataclass(frozen=True)
class OwnerQueuePreparationRequest:  # pylint: disable=too-many-instance-attributes
    """Run decisions used while constructing one owner queue."""

    paths: OwnerQueuePreparationPaths
    rest_first: str
    request_limit: int
    current_owner: str
    include_manual: bool
    now: int
    batch_marker: str


@dataclass(frozen=True)
class OwnerQueuePreparationResult:
    """Counts produced by one completed queue preparation."""

    candidates: int
    queued: int
    missing: int
    attempted_owners: tuple[str, ...]
    may_have_more: bool


@dataclass(frozen=True)
class _CandidateSelection:
    """One bounded selection and its SQLite filtering counts."""

    candidates: list[tuple[str, str]]
    windows: int
    known: int


@dataclass(frozen=True)
class OwnerQueuePreparationServices:
    """Stateful services used while preparing an owner queue."""

    repository: DurableOwnerQueueRepository
    resolver: OwnerCandidateResolver
    state: StateStore
    retire_owner: RetireOwner


@dataclass(frozen=True)
class OwnerQueuePreparationExecution:
    """Runtime callbacks and ordering source for queue preparation."""

    check_stop: StopCheck
    progress: MessageSink
    generator: random.Random | None = None


class OwnerQueuePreparationService:  # pylint: disable=too-few-public-methods
    """Normalize discovery inputs, resolve identities, and persist queue effects."""

    def __init__(
        self,
        services: OwnerQueuePreparationServices,
        execution: OwnerQueuePreparationExecution,
    ) -> None:
        self.services = services
        self.execution = execution

    def prepare(
        self,
        request: OwnerQueuePreparationRequest,
    ) -> OwnerQueuePreparationResult:
        """Build and persist the next bounded owner queue."""

        if request.request_limit < 0:
            raise ValueError("owner request limit cannot be negative")
        if request.now < 0:
            raise ValueError("queue preparation time cannot be negative")

        self.execution.check_stop()
        deferred = self.services.repository.deferred_owners(request.now)
        for owner, retry_after in deferred:
            self.execution.progress(
                f"Deferred {owner} until {_utc_timestamp(retry_after)}"
            )

        connections, selected, capacity = self._select(request, deferred)
        self.execution.check_stop()
        resolved, missing = self.services.resolver.resolve_candidates(
            owner for owner, _reason in selected
        )
        self._record_discovered(resolved, connections)
        missing_count = self._retire_missing(missing)
        queued = self._queue_resolved(
            resolved,
            selected,
            request.batch_marker,
            request.now,
        )
        return OwnerQueuePreparationResult(
            candidates=len(selected),
            queued=queued,
            missing=missing_count,
            attempted_owners=_unique_owner_logins(
                (owner for owner, _reason in selected),
                resolved,
            ),
            may_have_more=capacity > 0 and len(selected) == capacity,
        )

    def _select(
        self,
        request: OwnerQueuePreparationRequest,
        deferred: tuple[tuple[str, int], ...],
    ) -> tuple[tuple[str, ...], list[tuple[str, str]], int]:
        started_at = time.monotonic()
        paths = request.paths
        connections = _prepare_connections(paths)
        _prepare_manual_owners(paths)
        selector = OwnerQueueSelector(
            rest_first=request.rest_first,
            request_limit=request.request_limit,
            current_owner=request.current_owner,
            paths=OwnerQueuePaths(
                connections_file=paths.connections,
                manual_file=paths.manual_owners,
                index_dir=paths.index_directory,
                state_dir=paths.working_directory,
            ),
            include_manual=request.include_manual,
            deferred_owners=tuple(owner for owner, _retry_after in deferred),
        )
        selection = self._select_unattempted(selector, request.batch_marker)
        elapsed = max(0.0, time.monotonic() - started_at)
        self.execution.progress(
            "Owner candidate selection: "
            f"windows={selection.windows} known={selection.known} "
            f"selected={len(selection.candidates)} in {elapsed:.3f}s; "
            f"peak_rss={peak_resident_memory_mib():.1f}MiB"
        )
        return connections, selection.candidates, selector.capacity

    def _select_unattempted(
        self,
        selector: OwnerQueueSelector,
        batch_marker: str,
    ) -> _CandidateSelection:
        selected: list[tuple[str, str]] = []
        selected_keys: set[str] = set()
        windows = 0
        known_count = 0
        capacity_reached = False
        for batch in selector.candidate_batches(self.execution.generator):
            self.execution.check_stop()
            windows += 1
            known = self.services.repository.known_owner_queue_candidates(
                batch_marker,
                tuple(owner for owner, _reason in batch),
            )
            known_count += len(known)
            for owner, reason in batch:
                owner_key = _owner_key(owner)
                if (owner_key in known and reason != "manual") or (
                    owner_key in selected_keys
                ):
                    continue
                selected.append((owner, reason))
                selected_keys.add(owner_key)
                if len(selected) == selector.capacity:
                    capacity_reached = True
                    break
            if capacity_reached:
                break
        return _CandidateSelection(selected, windows, known_count)

    def _record_discovered(
        self,
        resolved: tuple[str, ...],
        connections: tuple[str, ...],
    ) -> None:
        connection_owners = {_owner_key(owner) for owner in connections}
        discovered = tuple(
            owner_ref
            for owner_ref in resolved
            if _owner_key(owner_ref) in connection_owners
        )
        for _owner_ref in discovered:
            self.execution.check_stop()
        self.services.state.add_many_to_set(
            StateKey.DISCOVERED_CONNECTION_OWNERS,
            discovered,
        )

    def _queue_resolved(
        self,
        resolved: tuple[str, ...],
        selected: list[tuple[str, str]],
        batch_marker: str,
        now: int,
    ) -> int:
        reason_by_owner: dict[str, str] = {}
        for owner, reason in selected:
            reason_by_owner.setdefault(_owner_key(owner), reason)
        for _owner_ref in resolved:
            self.execution.check_stop()
        admissions = tuple(
            _owner_admission(
                owner_ref,
                reason_by_owner.get(_owner_key(owner_ref), "discovered"),
            )
            for owner_ref in resolved
        )
        started_at = time.monotonic()
        added = self.services.repository.record_owner_queue_candidates(
            batch_marker,
            tuple(OwnerQueueCandidate(owner, reason) for owner, reason in selected),
            admissions,
            now,
        )
        elapsed = max(0.0, time.monotonic() - started_at)
        self.execution.progress(
            "Owner queue admission: "
            f"attempted={len(selected)} resolved={len(admissions)} "
            f"added={len(added)} sqlite={elapsed:.3f}s"
        )
        for entry in added:
            self.execution.progress(f"Queued {entry.owner} (reason: {entry.reason})")
        return len(added)

    def _retire_missing(self, missing: tuple[str, ...]) -> int:
        missing_owners = sorted(set(missing))
        for owner in missing_owners:
            self.execution.check_stop()
            self.services.retire_owner(owner)
        return len(missing_owners)


@dataclass(frozen=True)
class TargetedOwnerQueueResult:
    """Counts produced when queueing one owner and its memberships."""

    candidates: int
    queued: int
    missing: int


@dataclass(frozen=True)
class TargetedOwnerQueueServices:
    """Stateful services used by targeted owner admission."""

    repository: DurableOwnerQueueRepository
    resolver: OwnerCandidateResolver
    state: StateStore


class TargetedOwnerQueueService:  # pylint: disable=too-few-public-methods
    """Resolve and queue every owner selected by a targeted update mode."""

    def __init__(
        self,
        services: TargetedOwnerQueueServices,
        check_stop: StopCheck,
        progress: MessageSink,
    ) -> None:
        self.services = services
        self.check_stop = check_stop
        self.progress = progress

    def prepare(
        self,
        current_owner: str,
        connections_path: Path,
        batch_marker: str,
        now: int,
    ) -> TargetedOwnerQueueResult:
        """Persist all resolvable configured-owner and membership candidates."""

        return self._prepare(
            normalize_owner_lines((current_owner, *_read_lines(connections_path))),
            batch_marker,
            "targeted",
            now,
        )

    def prepare_optouts(
        self,
        optout_path: Path,
        batch_marker: str,
        now: int,
    ) -> TargetedOwnerQueueResult:
        """Persist every resolvable owner named by an opt-out entry."""

        return self._prepare(
            normalize_owner_lines(
                line.split("/", maxsplit=1)[0] for line in _read_lines(optout_path)
            ),
            batch_marker,
            "optout",
            now,
        )

    def _prepare(
        self,
        candidates: tuple[str, ...],
        batch_marker: str,
        reason: str,
        now: int,
    ) -> TargetedOwnerQueueResult:
        self.check_stop()
        resolved, missing = self.services.resolver.resolve_candidates(candidates)
        for _owner_ref in resolved:
            self.check_stop()
        added = self.services.repository.admit_owner_queue(
            batch_marker,
            tuple(_owner_admission(owner_ref, reason) for owner_ref in resolved),
            now,
        )
        for entry in added:
            self.progress(f"Queued {entry.owner}")
        return TargetedOwnerQueueResult(len(candidates), len(added), len(missing))


def _owner_admission(owner_ref: str, reason: str) -> OwnerQueueAdmission:
    owner_id, separator, owner = owner_ref.partition("/")
    if not separator:
        raise ValueError(f"invalid resolved owner reference: {owner_ref}")
    return OwnerQueueAdmission(owner_id, owner, reason)


def _prepare_connections(paths: OwnerQueuePreparationPaths) -> tuple[str, ...]:
    lines = _read_lines(paths.connections)
    counts = Counter(lines)
    ordered = sorted(counts, key=lambda value: (counts[value], value), reverse=True)
    scanned_without_packages = set(
        _read_lines(paths.working_directory / RunFile.OWNERS_SCANNED_WITHOUT_PACKAGES)
    )
    connections = normalize_owner_lines(
        line for line in ordered if line not in scanned_without_packages
    )
    _write_lines(paths.connections, connections)
    return connections


def _prepare_manual_owners(paths: OwnerQueuePreparationPaths) -> tuple[str, ...]:
    known = set(_read_lines(paths.working_directory / RunFile.ALL_OWNERS_IN_DB))
    owners = tuple(
        owner
        for owner in normalize_owner_lines(_read_lines(paths.manual_owners))
        if owner not in known
    )
    _write_lines(paths.manual_owners, owners)
    return owners


def _owner_login(value: str) -> str:
    return value.split("/", maxsplit=1)[-1]


def _unique_owner_logins(*sources: Iterable[str]) -> tuple[str, ...]:
    owners: list[str] = []
    seen: set[str] = set()
    for value in (value for source in sources for value in source):
        owner = _owner_login(value)
        key = owner.casefold()
        if key in seen:
            continue
        seen.add(key)
        owners.append(owner)
    return tuple(owners)


def _owner_key(value: str) -> str:
    return _owner_login(value).casefold()


def _utc_timestamp(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_lines(path: Path) -> tuple[str, ...]:
    try:
        return tuple(path.read_text(encoding="utf-8").splitlines())
    except FileNotFoundError:
        return ()


def _write_lines(path: Path, lines: Iterable[str]) -> None:
    values = tuple(lines)
    path.parent.mkdir(parents=True, exist_ok=True)
    with atomic_text_output(path) as output:
        if values:
            output.write("\n".join(values))
            output.write("\n")
