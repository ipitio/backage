"""Tests for Python-owned post-discovery owner queue preparation."""

from __future__ import annotations

import random
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from bkg_py.database.composition import DatabaseRepositories
from bkg_py.database.owner_queue import (
    OwnerQueueAdmission,
    OwnerQueueCandidate,
    OwnerQueueCompletion,
    OwnerQueueEntry,
)
from bkg_py.database.settings import DatabaseSettings
from bkg_py.owners.batch import OwnerBatchEffects
from bkg_py.owners.queue import OwnerQueuePaths, OwnerQueueSelector
from bkg_py.owners.queue_operations import (
    OwnerQueuePreparationExecution,
    OwnerQueuePreparationPaths,
    OwnerQueuePreparationRequest,
    OwnerQueuePreparationService,
    OwnerQueuePreparationServices,
    TargetedOwnerQueueService,
    TargetedOwnerQueueServices,
)
from bkg_py.state import StateStore

from ..workspace.repository_support import create_repository, git


@dataclass
class _Repository:
    database: DatabaseRepositories
    retired: list[str] = field(default_factory=list[str])

    def deferred_owners(self, now: int) -> tuple[tuple[str, int], ...]:
        """Return one owner whose retry window remains active."""

        del now
        return (("deferred", 1_788_739_200),)

    def retire_owner(self, owner: str) -> int:
        """Record one authoritatively missing owner."""

        self.retired.append(owner)
        return self.database.packages.retire_owner(owner)

    def admit_owner_queue(
        self,
        generation: str,
        admissions: tuple[OwnerQueueAdmission, ...],
        now: int,
    ) -> tuple[OwnerQueueEntry, ...]:
        """Delegate owner admission to the production repository."""

        return self.database.owner_queue.admit_owner_queue(generation, admissions, now)

    def known_owner_queue_candidates(
        self,
        generation: str,
        candidates: tuple[str, ...],
    ) -> frozenset[str]:
        """Delegate bounded candidate lookups to the production repository."""

        return self.database.owner_queue.known_owner_queue_candidates(
            generation, candidates
        )

    def record_owner_queue_candidates(
        self,
        generation: str,
        candidates: tuple[OwnerQueueCandidate, ...],
        admissions: tuple[OwnerQueueAdmission, ...],
        now: int,
    ) -> tuple[OwnerQueueEntry, ...]:
        """Delegate atomic candidate and canonical owner admission."""

        return self.database.owner_queue.record_owner_queue_candidates(
            generation,
            candidates,
            admissions,
            now,
        )

    def owner_queue_entries(
        self,
        generation: str,
        *,
        status: str | None = None,
    ) -> tuple[OwnerQueueEntry, ...]:
        """Delegate ordered queue reads to the production repository."""

        return self.database.owner_queue.owner_queue_entries(generation, status=status)

    def claim_owner_queue_wave(
        self,
        generation: str,
        limit: int,
        claim_token: str,
        now: int,
    ) -> tuple[OwnerQueueEntry, ...]:
        """Delegate bounded claims to the production repository."""

        return self.database.owner_queue.claim_owner_queue_wave(
            generation, limit, claim_token, now
        )

    def finish_owner_queue_claim(self, completion: OwnerQueueCompletion) -> None:
        """Delegate parent-owned completion to the production repository."""

        self.database.owner_queue.finish_owner_queue_claim(completion)


def _repository(tmp_path: Path) -> _Repository:
    database = DatabaseRepositories(DatabaseSettings(tmp_path / "index.db"))
    database.owner_queue.prepare_owner_queue("batch-1", (), 1)
    return _Repository(database)


@dataclass
class _Resolver:
    candidates: tuple[str, ...] = ()

    def resolve_candidates(
        self,
        candidates: Iterable[str],
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Resolve known fixture candidates and report one missing owner."""

        self.candidates = tuple(candidates)
        resolved: list[str] = []
        missing: list[str] = []
        identities = {
            "manual": "1/Manual",
            "service": "2/service",
            "alpha": "3/Alpha",
            "beta": "99/Beta",
        }
        for candidate in self.candidates:
            login = candidate.split("/", maxsplit=1)[-1]
            if login == "missing":
                missing.append(login)
            elif login.casefold() in identities:
                resolved.append(identities[login.casefold()])
        return tuple(resolved), tuple(missing)


def _write_lines(path: Path, *lines: str) -> None:
    """Write one line-oriented fixture file."""

    path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")


def _no_history(_selector: OwnerQueueSelector) -> list[str]:
    """Keep queue selection independent of the test repository history."""

    return []


def test_history_owners_ignores_generated_index_directories(tmp_path: Path) -> None:
    """Static-site roots cannot become owner-discovery candidates."""

    index = tmp_path / "index"
    create_repository(index)
    site = index / ".bkg-site" / "candidate" / "index.html"
    site.parent.mkdir(parents=True)
    site.write_text("site shell\n", encoding="utf-8")
    git(index, "add", "-A")
    git(index, "commit", "-qm", "publish site shell")
    selector = OwnerQueueSelector(
        rest_first="0",
        request_limit=1,
        current_owner="",
        paths=OwnerQueuePaths(
            tmp_path / "connections",
            tmp_path / "owners.txt",
            index,
            tmp_path / "state",
        ),
        deferred_owners=(),
    )

    assert selector.history_owners() == ["alpha", "beta"]


def test_queue_selection_reserves_capacity_for_discovery_before_stale_backlog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale backlog cannot consume the complete daily discovery pass."""

    working = tmp_path / "working"
    index = tmp_path / "index"
    working.mkdir()
    index.mkdir()
    connections = tmp_path / "connections"
    owners = tmp_path / "owners.txt"
    _write_lines(connections, *(f"discovered-{index}" for index in range(10)))
    _write_lines(owners)
    _write_lines(working / "all_owners_in_db", "partial", "stale")
    _write_lines(working / "owners_partially_updated", "partial")
    _write_lines(working / "owners_stale", "stale")
    monkeypatch.setattr(OwnerQueueSelector, "history_owners", _no_history)

    selector = OwnerQueueSelector(
        rest_first="0",
        request_limit=1,
        current_owner="",
        paths=OwnerQueuePaths(connections, owners, index, working),
        deferred_owners=(),
    )

    selected = selector.select_with_reasons(random.Random(0))  # noqa: S311

    assert selected[0] == ("discovered-0", "connection")
    assert {owner for owner, _reason in selected[1:3]} == {"partial", "stale"}
    assert {reason for _owner, reason in selected[1:3]} == {
        "partially-updated",
        "stale",
    }
    assert len(selected) == 4

    batches = tuple(selector.candidate_batches(random.Random(0)))  # noqa: S311

    assert all(0 < len(batch) <= selector.capacity for batch in batches)
    assert len(batches) > 1


def test_queue_preparation_reports_and_advances_a_full_chunk(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Continuation metadata advances beyond every attempted candidate."""

    working = tmp_path / "working"
    index = tmp_path / "index"
    working.mkdir()
    index.mkdir()
    connections = tmp_path / "connections"
    owners = tmp_path / "owners.txt"
    discovered = tuple(f"owner-{number}" for number in range(5))
    _write_lines(connections, *discovered)
    _write_lines(owners)
    monkeypatch.setattr(OwnerQueueSelector, "history_owners", _no_history)
    state = StateStore(tmp_path / "state.env")
    repository = _repository(tmp_path)
    service = OwnerQueuePreparationService(
        OwnerQueuePreparationServices(
            repository,
            _Resolver(),
            state,
            lambda _owner: None,
        ),
        OwnerQueuePreparationExecution(
            lambda: None,
            lambda _message: None,
            random.Random(0),  # noqa: S311
        ),
    )
    paths = OwnerQueuePreparationPaths(connections, owners, index, working)

    first = service.prepare(
        OwnerQueuePreparationRequest(paths, "0", 1, "", True, 1_788_652_800, "batch-1")
    )
    second = service.prepare(
        OwnerQueuePreparationRequest(
            paths,
            "0",
            1,
            "",
            True,
            1_788_652_801,
            "batch-1",
        )
    )

    assert first.candidates == 4
    assert first.may_have_more
    assert second.candidates == 1
    assert not second.may_have_more
    assert set(first.attempted_owners).isdisjoint(second.attempted_owners)


def test_manual_request_promotes_an_attempted_connection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Candidate dedupe cannot suppress a later explicit owner request."""

    working = tmp_path / "working"
    index = tmp_path / "index"
    working.mkdir()
    index.mkdir()
    connections = tmp_path / "connections"
    owners = tmp_path / "owners.txt"
    _write_lines(connections, "alpha")
    _write_lines(owners)
    monkeypatch.setattr(OwnerQueueSelector, "history_owners", _no_history)
    repository = _repository(tmp_path)
    service = OwnerQueuePreparationService(
        OwnerQueuePreparationServices(
            repository,
            _Resolver(),
            StateStore(tmp_path / "state.env"),
            lambda _owner: None,
        ),
        OwnerQueuePreparationExecution(lambda: None, lambda _message: None),
    )
    request = OwnerQueuePreparationRequest(
        OwnerQueuePreparationPaths(connections, owners, index, working),
        "0",
        100,
        "",
        True,
        1_788_652_800,
        "batch-1",
    )

    service.prepare(request)
    _write_lines(owners, "alpha")
    service.prepare(request)

    entry = repository.owner_queue_entries("batch-1")[0]
    assert entry.owner == "Alpha"
    assert entry.reason == "manual"
    assert entry.priority == 0


def test_queue_preparation_owns_normalization_resolution_and_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One operation persists queue state without shell temporary files."""

    working = tmp_path / "working"
    index = tmp_path / "index"
    working.mkdir()
    index.mkdir()
    connections = tmp_path / "connections"
    owners = tmp_path / "owners.txt"
    _write_lines(
        connections,
        "alpha",
        "skip",
        "alpha",
        "99/Beta",
        '"enterprise"',
        "null/invalid",
    )
    _write_lines(owners, " Known ", "manual", "missing", "manual")
    _write_lines(working / "all_owners_in_db", "Known")
    _write_lines(working / "owners_scanned_without_packages", "skip")
    for name in ("owners_stale", "owners_partially_updated"):
        _write_lines(working / name)
    (index / "missing").mkdir()
    monkeypatch.setattr(OwnerQueueSelector, "history_owners", _no_history)

    state = StateStore(tmp_path / "state.env")
    repository = _repository(tmp_path)
    repository.database.owner_queue.admit_owner_queue(
        "batch-1",
        (OwnerQueueAdmission("2", "service", "legacy"),),
        2,
    )
    resolver = _Resolver()
    messages: list[str] = []
    effects = OwnerBatchEffects(
        repository,
        repository,
        state,
        owners,
        index,
        messages.append,
    )
    service = OwnerQueuePreparationService(
        OwnerQueuePreparationServices(
            repository,
            resolver,
            state,
            effects.retire_unavailable,
        ),
        OwnerQueuePreparationExecution(
            lambda: None,
            messages.append,
            random.Random(0),  # noqa: S311 - deterministic queue ordering fixture
        ),
    )

    result = service.prepare(
        OwnerQueuePreparationRequest(
            paths=OwnerQueuePreparationPaths(
                connections,
                owners,
                index,
                working,
            ),
            rest_first="0",
            request_limit=100,
            current_owner="service",
            include_manual=True,
            now=1_788_652_800,
            batch_marker="batch-1",
        )
    )

    assert result.candidates == 5
    assert result.queued == 3
    assert result.missing == 1
    assert set(result.attempted_owners) == {
        "manual",
        "missing",
        "service",
        "alpha",
        "Beta",
    }
    assert not result.may_have_more
    assert set(resolver.candidates) == {
        "manual",
        "missing",
        "service",
        "alpha",
        "99/Beta",
    }
    assert [
        entry.ref
        for entry in repository.database.owner_queue.owner_queue_entries("batch-1")
    ] == [
        "1/Manual",
        "3/Alpha",
        "99/Beta",
        "2/service",
    ]
    assert state.get_set("BKG_DISCOVERED_CONNECTION_OWNERS") == [
        "3/Alpha",
        "99/Beta",
    ]
    assert connections.read_text(encoding="utf-8") == "alpha\n99/Beta\n"
    assert owners.read_text(encoding="utf-8") == "manual\n"
    assert repository.retired == ["missing"]
    assert not (index / "missing").exists()
    assert "Queued Manual (reason: manual)" in messages
    assert "Queued Alpha (reason: connection)" in messages
    assert "Queued Beta (reason: connection)" in messages
    assert "Retired unavailable owner missing" in messages
    assert any(
        message.startswith("Owner candidate selection: windows=")
        for message in messages
    )
    assert any(
        message.startswith(
            "Owner queue admission: attempted=5 resolved=4 added=3 sqlite="
        )
        for message in messages
    )
    assert any(message.startswith("Deferred deferred until ") for message in messages)


def test_targeted_owner_queue_resolves_configured_owner_and_memberships(
    tmp_path: Path,
) -> None:
    """Targeted modes queue every resolvable owner without global selection."""

    connections = tmp_path / "connections"
    _write_lines(connections, "alpha", "99/Beta", "alpha", "missing")
    state = StateStore(tmp_path / "state.env")
    repository = _repository(tmp_path)
    resolver = _Resolver()
    messages: list[str] = []
    service = TargetedOwnerQueueService(
        TargetedOwnerQueueServices(repository, resolver, state),
        lambda: None,
        messages.append,
    )

    result = service.prepare("service", connections, "batch-1", 100)

    assert result.candidates == 4
    assert result.queued == 3
    assert result.missing == 1
    assert resolver.candidates == ("service", "alpha", "99/Beta", "missing")
    assert [
        entry.ref
        for entry in repository.database.owner_queue.owner_queue_entries("batch-1")
    ] == [
        "2/service",
        "3/Alpha",
        "99/Beta",
    ]
    assert messages == ["Queued service", "Queued Alpha", "Queued Beta"]


def test_targeted_owner_queue_extracts_and_resolves_optout_owners(
    tmp_path: Path,
) -> None:
    """The fast opt-out path batches unique owners from component entries."""

    optouts = tmp_path / "optout.txt"
    _write_lines(
        optouts,
        "alpha/repository/package",
        "beta/repository/package",
        "alpha/other/package",
        "missing/repository/package",
    )
    state = StateStore(tmp_path / "state.env")
    repository = _repository(tmp_path)
    resolver = _Resolver()
    service = TargetedOwnerQueueService(
        TargetedOwnerQueueServices(repository, resolver, state),
        lambda: None,
        lambda _message: None,
    )

    result = service.prepare_optouts(optouts, "batch-1", 100)

    assert result.candidates == 3
    assert result.queued == 2
    assert result.missing == 1
    assert resolver.candidates == ("alpha", "beta", "missing")
    assert tuple(
        entry.ref
        for entry in repository.database.owner_queue.owner_queue_entries("batch-1")
    ) == ("3/Alpha", "99/Beta")
