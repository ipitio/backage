"""Coordinate one pooled owner-discovery phase."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .files import atomic_text_output
from .owner_pages import OwnerPageAdmissionResult
from .owner_queue import normalize_owner_lines

MessageSink = Callable[[str], None]
StopCheck = Callable[[], None]
Clock = Callable[[], float]
OwnerPageAdmitter = Callable[[int, int], OwnerPageAdmissionResult]
CompleteExploreGate = Callable[[str], None]


class DiscoveryTraversal(Protocol):
    """Traversals needed by the outer discovery phase."""

    def explore(self, node: str, edge: str = "") -> tuple[str, ...]:
        """Return connected owners for one user, organization, or repository."""

        raise NotImplementedError

    def organization_logins(self, owner_ref: str) -> tuple[str, ...]:
        """Return organizations associated with one owner."""

        raise NotImplementedError

    def membership(self, owner_ref: str) -> tuple[str, ...]:
        """Return direct membership discovery for one service owner."""

        raise NotImplementedError


@dataclass(frozen=True)
class DiscoveryPhasePaths:
    """Files read or replaced by the discovery phase."""

    connections: Path
    owners: Path
    optout: Path


@dataclass(frozen=True)
class DiscoveryPhaseIdentity:
    """Repository identity and deployment role for discovery."""

    owner: str
    repository: str
    primary_service: bool


@dataclass(frozen=True)
class DiscoveryPhaseRequest:
    """Run context for one authenticated discovery phase."""

    paths: DiscoveryPhasePaths
    identity: DiscoveryPhaseIdentity
    today: str
    skip_explore: bool
    first_run: bool
    owner_page_limit: int


@dataclass(frozen=True)
class DiscoveryPhaseResult:
    """Work completed by one authenticated discovery phase."""

    connections: int
    owner_pages: int
    admitted_owners: int


@dataclass(frozen=True)
class DiscoveryPhaseServices:
    """Stateful operations used by discovery orchestration."""

    traversal: DiscoveryTraversal
    admit_owner_page: OwnerPageAdmitter
    complete_explore_gate: CompleteExploreGate


@dataclass(frozen=True)
class DiscoveryPhaseExecution:
    """Runtime callbacks used by discovery orchestration."""

    check_stop: StopCheck
    progress: MessageSink
    clock: Clock = time.monotonic


class DiscoveryPhaseService:  # pylint: disable=too-few-public-methods
    """Run discovery without crossing the shell boundary per page."""

    def __init__(
        self,
        services: DiscoveryPhaseServices,
        execution: DiscoveryPhaseExecution,
    ) -> None:
        self.services = services
        self.execution = execution

    def run(self, request: DiscoveryPhaseRequest) -> DiscoveryPhaseResult:
        """Discover connections and admit global owners for one run."""

        if request.owner_page_limit < 0:
            raise ValueError("owner discovery page limit cannot be negative")
        self.execution.check_stop()
        if request.identity.primary_service:
            return self._run_primary(request)
        return self._run_membership(request)

    def _run_primary(self, request: DiscoveryPhaseRequest) -> DiscoveryPhaseResult:
        if request.skip_explore:
            self.execution.progress("Skipping explore; already ran today")
            connections: tuple[str, ...] = ()
            _write_lines(request.paths.connections, connections)
        else:
            connections = self._discover_connections(request.identity)
            _write_lines(request.paths.connections, connections)
        self.services.complete_explore_gate(request.today)
        pages, admitted = self._admit_owner_pages(request, connections)
        return DiscoveryPhaseResult(len(connections), pages, admitted)

    def _discover_connections(
        self,
        identity: DiscoveryPhaseIdentity,
    ) -> tuple[str, ...]:
        started_at = self.execution.clock()
        connections = normalize_owner_lines(
            (
                *self.services.traversal.explore(identity.owner),
                *self.services.traversal.explore(
                    f"{identity.owner}/{identity.repository}"
                ),
            )
        )
        self._log_phase("discover-connections", started_at)

        started_at = self.execution.clock()
        expanded = list(connections)
        for connection in connections:
            self.execution.check_stop()
            expanded.extend(self.services.traversal.organization_logins(connection))
        result = normalize_owner_lines(expanded)
        self._log_phase("expand-connection-orgs", started_at)
        return result

    def _admit_owner_pages(
        self,
        request: DiscoveryPhaseRequest,
        connections: tuple[str, ...],
    ) -> tuple[int, int]:
        started_at = self.execution.clock()
        owners_count = _line_count(request.paths.owners)
        per_page = 100 if owners_count < len(set(connections)) + 100 else 1
        pages = 0
        admitted = 0
        for page_number in range(1, request.owner_page_limit + 1):
            self.execution.check_stop()
            self.execution.progress(f"Checking owners page {page_number}...")
            result = self.services.admit_owner_page(page_number, per_page)
            pages += 1
            admitted += result.admitted_count
            for owner in result.requested_logins:
                self.execution.progress(f"Requested {owner}")
            self.execution.progress(f"Checked owners page {page_number}")
            if not result.has_more:
                break
        self._log_phase("page-owner-discovery", started_at)
        return pages, admitted

    def _run_membership(
        self,
        request: DiscoveryPhaseRequest,
    ) -> DiscoveryPhaseResult:
        started_at = self.execution.clock()
        connections = normalize_owner_lines(
            self.services.traversal.membership(request.identity.owner)
        )
        _write_lines(request.paths.connections, connections)
        if request.first_run:
            _write_lines(request.paths.owners, ())
            _write_lines(request.paths.optout, ())
        self._log_phase("discover-membership", started_at)
        return DiscoveryPhaseResult(len(connections), 0, 0)

    def _log_phase(self, phase: str, started_at: float) -> None:
        elapsed = max(0, int(self.execution.clock() - started_at))
        self.execution.progress(f"Startup phase '{phase}' completed in {elapsed}s")


def _line_count(path: Path) -> int:
    try:
        return path.read_text(encoding="utf-8").count("\n")
    except FileNotFoundError:
        return 0


def _write_lines(path: Path, lines: tuple[str, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with atomic_text_output(path) as output:
        if lines:
            output.write("\n".join(lines))
            output.write("\n")
