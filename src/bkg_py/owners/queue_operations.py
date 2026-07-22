"""Prepare the durable owner queue after global discovery completes."""

from __future__ import annotations

import random
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from ..files import atomic_text_output
from ..state import StateStore
from .queue import OwnerQueuePaths, OwnerQueueSelector, normalize_owner_lines

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


class DeferredOwnerRepository(Protocol):  # pylint: disable=too-few-public-methods
    """Database read needed to exclude owners still under retry backoff."""

    def deferred_owners(self, now: int) -> tuple[tuple[str, int], ...]:
        """Return owner names and future retry timestamps."""

        raise NotImplementedError


@dataclass(frozen=True)
class OwnerQueuePreparationPaths:
    """Files consulted while constructing one owner queue."""

    connections: Path
    manual_owners: Path
    index_directory: Path
    working_directory: Path


@dataclass(frozen=True)
class OwnerQueuePreparationRequest:
    """Run decisions used while constructing one owner queue."""

    paths: OwnerQueuePreparationPaths
    rest_first: str
    request_limit: int
    current_owner: str
    include_manual: bool
    now: int
    excluded_owners: tuple[str, ...] = ()


@dataclass(frozen=True)
class OwnerQueuePreparationResult:
    """Counts produced by one completed queue preparation."""

    candidates: int
    queued: int
    missing: int
    attempted_owners: tuple[str, ...]
    may_have_more: bool


@dataclass(frozen=True)
class OwnerQueuePreparationServices:
    """Stateful services used while preparing an owner queue."""

    repository: DeferredOwnerRepository
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
        queued = self._queue_resolved(resolved, selected)
        missing_count = self._retire_missing(missing)

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
            excluded_owners=request.excluded_owners,
        )
        return (
            connections,
            selector.select_with_reasons(self.execution.generator),
            selector.capacity,
        )

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
            "BKG_DISCOVERED_CONNECTION_OWNERS",
            discovered,
        )

    def _queue_resolved(
        self,
        resolved: tuple[str, ...],
        selected: list[tuple[str, str]],
    ) -> int:
        reason_by_owner: dict[str, str] = {}
        for owner, reason in selected:
            reason_by_owner.setdefault(_owner_key(owner), reason)
        for _owner_ref in resolved:
            self.execution.check_stop()
        added = self.services.state.add_many_to_set("BKG_OWNERS_QUEUE", resolved)
        for owner_ref in added:
            owner = _owner_login(owner_ref)
            reason = reason_by_owner.get(owner.casefold(), "discovered")
            self.execution.progress(f"Queued {owner} (reason: {reason})")
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


class TargetedOwnerQueueService:  # pylint: disable=too-few-public-methods
    """Resolve and queue every owner selected by a targeted update mode."""

    def __init__(
        self,
        resolver: OwnerCandidateResolver,
        state: StateStore,
        check_stop: StopCheck,
        progress: MessageSink,
    ) -> None:
        self.resolver = resolver
        self.state = state
        self.check_stop = check_stop
        self.progress = progress

    def prepare(
        self,
        current_owner: str,
        connections_path: Path,
    ) -> TargetedOwnerQueueResult:
        """Persist all resolvable configured-owner and membership candidates."""

        return self._prepare(
            normalize_owner_lines((current_owner, *_read_lines(connections_path)))
        )

    def prepare_optouts(self, optout_path: Path) -> TargetedOwnerQueueResult:
        """Persist every resolvable owner named by an opt-out entry."""

        return self._prepare(
            normalize_owner_lines(
                line.split("/", maxsplit=1)[0] for line in _read_lines(optout_path)
            )
        )

    def _prepare(
        self,
        candidates: tuple[str, ...],
    ) -> TargetedOwnerQueueResult:
        self.check_stop()
        resolved, missing = self.resolver.resolve_candidates(candidates)
        for _owner_ref in resolved:
            self.check_stop()
        added = self.state.add_many_to_set("BKG_OWNERS_QUEUE", resolved)
        for owner_ref in added:
            self.progress(f"Queued {_owner_login(owner_ref)}")
        return TargetedOwnerQueueResult(len(candidates), len(added), len(missing))


def _prepare_connections(paths: OwnerQueuePreparationPaths) -> tuple[str, ...]:
    lines = _read_lines(paths.connections)
    counts = Counter(lines)
    ordered = sorted(counts, key=lambda value: (counts[value], value), reverse=True)
    scanned_without_packages = set(
        _read_lines(paths.working_directory / "owners_scanned_without_packages")
    )
    connections = normalize_owner_lines(
        line for line in ordered if line not in scanned_without_packages
    )
    _write_lines(paths.connections, connections)
    return connections


def _prepare_manual_owners(paths: OwnerQueuePreparationPaths) -> tuple[str, ...]:
    known = set(_read_lines(paths.working_directory / "all_owners_in_db"))
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
