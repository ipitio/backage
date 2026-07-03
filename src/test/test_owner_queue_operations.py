"""Tests for Python-owned post-discovery owner queue preparation."""

from __future__ import annotations

import random
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from bkg_py.owner_batch import OwnerBatchEffects
from bkg_py.owner_queue import OwnerQueueSelector
from bkg_py.owner_queue_operations import (
    OwnerQueuePreparationExecution,
    OwnerQueuePreparationPaths,
    OwnerQueuePreparationRequest,
    OwnerQueuePreparationService,
    OwnerQueuePreparationServices,
)
from bkg_py.state import StateStore


@dataclass
class _Repository:
    retired: list[str] = field(default_factory=list[str])

    def deferred_owners(self, now: int) -> tuple[tuple[str, int], ...]:
        """Return one owner whose retry window remains active."""

        del now
        return (("deferred", 1_788_739_200),)

    def retire_owner(self, owner: str) -> int:
        """Record one authoritatively missing owner."""

        self.retired.append(owner)
        return 1


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
    state.add_to_set("BKG_OWNERS_QUEUE", "2/service")
    repository = _Repository()
    resolver = _Resolver()
    messages: list[str] = []
    effects = OwnerBatchEffects(repository, state, owners, index, messages.append)
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
        )
    )

    assert result.candidates == 5
    assert result.queued == 3
    assert result.missing == 1
    assert set(resolver.candidates) == {
        "manual",
        "missing",
        "service",
        "alpha",
        "99/Beta",
    }
    assert state.get_set("BKG_OWNERS_QUEUE") == [
        "2/service",
        "1/Manual",
        "3/Alpha",
        "99/Beta",
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
    assert any(message.startswith("Deferred deferred until ") for message in messages)
