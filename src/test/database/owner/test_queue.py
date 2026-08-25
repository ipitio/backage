"""Tests for the lazy generation-scoped SQLite owner queue."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from bkg_py.database.composition import DatabaseRepositories
from bkg_py.database.owner.queue import (
    OwnerQueueAdmission,
    OwnerQueueCandidate,
    OwnerQueueCompletion,
)
from bkg_py.database.settings import DatabaseSettings
from bkg_py.database.support import DatabaseError


def _repository(tmp_path: Path) -> DatabaseRepositories:
    return DatabaseRepositories(DatabaseSettings(tmp_path / "index.db"))


def test_legacy_import_is_idempotent_and_database_becomes_authoritative(
    tmp_path: Path,
) -> None:
    """Later legacy-state edits cannot replace an active SQLite queue."""

    repository = _repository(tmp_path)

    imported = repository.owner_queue.prepare_owner_queue(
        "batch-1",
        ("1/Alpha", "2/Beta"),
        100,
    )
    repeated = repository.owner_queue.prepare_owner_queue(
        "batch-1",
        ("3/Replacement",),
        101,
    )

    assert tuple(entry.ref for entry in imported) == ("1/Alpha", "2/Beta")
    assert tuple(entry.ref for entry in repeated) == ("1/Alpha", "2/Beta")
    with sqlite3.connect(tmp_path / "index.db") as connection:
        assert connection.execute(
            "select count(*) from bkg_owner_queue"
        ).fetchone() == (2,)


def test_admission_promotes_without_resequencing_or_demoting(tmp_path: Path) -> None:
    """A stronger reason changes priority while stable admission order remains."""

    repository = _repository(tmp_path)
    repository.owner_queue.prepare_owner_queue("batch-1", (), 100)
    repository.owner_queue.admit_owner_queue(
        "batch-1",
        (
            OwnerQueueAdmission("1", "Alpha", "connection"),
            OwnerQueueAdmission("2", "Beta", "stale"),
            OwnerQueueAdmission("3", "Gamma", "connection"),
        ),
        101,
    )
    repository.owner_queue.admit_owner_queue(
        "batch-1",
        (
            OwnerQueueAdmission("3", "Gamma", "manual"),
            OwnerQueueAdmission("2", "Beta", "index-history"),
        ),
        102,
    )

    entries = repository.owner_queue.owner_queue_entries("batch-1")

    assert tuple(entry.owner for entry in entries) == ("Gamma", "Alpha", "Beta")
    assert {entry.owner: entry.sequence for entry in entries} == {
        "Alpha": 0,
        "Beta": 1,
        "Gamma": 2,
    }
    assert {entry.owner: entry.reason for entry in entries} == {
        "Alpha": "connection",
        "Beta": "stale",
        "Gamma": "manual",
    }


def test_startup_lazily_normalizes_persisted_reason_priorities(
    tmp_path: Path,
) -> None:
    """An upgrade promotes queued connections without rebuilding the queue."""

    repository = _repository(tmp_path)
    repository.owner_queue.prepare_owner_queue("batch-1", (), 100)
    repository.owner_queue.admit_owner_queue(
        "batch-1",
        (
            OwnerQueueAdmission("1", "Connected", "connection"),
            OwnerQueueAdmission("2", "Stale", "stale"),
        ),
        101,
    )
    with sqlite3.connect(tmp_path / "index.db") as connection:
        connection.execute(
            "update bkg_owner_queue set priority = 40 where owner = 'Connected'"
        )

    repository.owner_queue.prepare_owner_queue("batch-1", (), 102)

    entries = repository.owner_queue.owner_queue_entries("batch-1")
    assert tuple((entry.owner, entry.priority) for entry in entries) == (
        ("Connected", 15),
        ("Stale", 20),
    )


def test_candidate_attempts_bound_continuation_and_reset_with_generation(
    tmp_path: Path,
) -> None:
    """Attempted and admitted logins are durable only for the active generation."""

    repository = _repository(tmp_path)
    repository.owner_queue.prepare_owner_queue("batch-1", (), 100)
    repository.owner_queue.record_owner_queue_candidates(
        "batch-1",
        (
            OwnerQueueCandidate("Alpha", "connection"),
            OwnerQueueCandidate("2/Missing", "connection"),
        ),
        (OwnerQueueAdmission("1", "Alpha", "connection"),),
        101,
    )

    assert repository.owner_queue.known_owner_queue_candidates(
        "batch-1",
        ("alpha", "Missing", "Unseen"),
    ) == frozenset({"alpha", "missing"})
    stats = repository.owner_queue.owner_queue_stats("batch-1")
    assert (
        stats.total,
        stats.ready,
        stats.claimed,
        stats.paused,
        stats.completed,
        stats.candidates,
    ) == (1, 1, 0, 0, 0, 2)
    repository.owner_queue.prepare_owner_queue("batch-2", (), 102)

    assert not repository.owner_queue.known_owner_queue_candidates(
        "batch-2",
        ("Alpha", "Missing"),
    )
    with sqlite3.connect(tmp_path / "index.db") as connection:
        assert connection.execute(
            "select count(*) from bkg_owner_queue_candidates"
        ).fetchone() == (0,)


def test_candidate_admission_reconciles_a_renamed_login(tmp_path: Path) -> None:
    """An old candidate login maps to one canonical queue identity."""

    repository = _repository(tmp_path)
    repository.owner_queue.prepare_owner_queue("batch-1", (), 100)

    added = repository.owner_queue.record_owner_queue_candidates(
        "batch-1",
        (OwnerQueueCandidate("OldLogin", "connection"),),
        (OwnerQueueAdmission("1", "NewLogin", "connection"),),
        101,
    )
    repeated = repository.owner_queue.record_owner_queue_candidates(
        "batch-1",
        (OwnerQueueCandidate("NewLogin", "stale"),),
        (OwnerQueueAdmission("1", "NewLogin", "stale"),),
        102,
    )

    assert tuple(entry.ref for entry in added) == ("1/NewLogin",)
    assert repeated == ()
    assert repository.owner_queue.known_owner_queue_candidates(
        "batch-1",
        ("OldLogin", "NewLogin"),
    ) == frozenset({"oldlogin", "newlogin"})
    assert tuple(
        entry.ref for entry in repository.owner_queue.owner_queue_entries("batch-1")
    ) == ("1/NewLogin",)


def test_candidate_and_queue_admission_roll_back_together(tmp_path: Path) -> None:
    """An interrupted canonical insert cannot strand an attempted candidate."""

    repository = _repository(tmp_path)
    repository.owner_queue.prepare_owner_queue("batch-1", (), 100)
    with sqlite3.connect(tmp_path / "index.db") as connection:
        connection.execute(
            """
            create trigger fail_owner_queue_admission
            before insert on bkg_owner_queue
            begin
                select raise(abort, 'simulated interruption');
            end
            """
        )

    with pytest.raises(DatabaseError, match="simulated interruption"):
        repository.owner_queue.record_owner_queue_candidates(
            "batch-1",
            (OwnerQueueCandidate("Alpha", "connection"),),
            (OwnerQueueAdmission("1", "Alpha", "connection"),),
            101,
        )

    assert not repository.owner_queue.known_owner_queue_candidates(
        "batch-1", ("Alpha",)
    )
    assert repository.owner_queue.owner_queue_entries("batch-1") == ()


def test_claims_are_bounded_completed_by_parent_and_paused_later(
    tmp_path: Path,
) -> None:
    """Only a bounded wave is claimed and paused work needs explicit activation."""

    repository = _repository(tmp_path)
    repository.owner_queue.prepare_owner_queue("batch-1", (), 100)
    repository.owner_queue.admit_owner_queue(
        "batch-1",
        tuple(
            OwnerQueueAdmission(str(index), owner, "connection")
            for index, owner in enumerate(("Alpha", "Beta", "Gamma"), start=1)
        ),
        101,
    )

    first = repository.owner_queue.claim_owner_queue_wave("batch-1", 2, "claim-1", 102)
    assert tuple(entry.owner for entry in first) == ("Alpha", "Beta")
    repository.owner_queue.finish_owner_queue_claim(
        OwnerQueueCompletion("batch-1", "1", "claim-1", "updated", 103)
    )
    repository.owner_queue.finish_owner_queue_claim(
        OwnerQueueCompletion("batch-1", "2", "claim-1", "paused", 103)
    )

    second = repository.owner_queue.claim_owner_queue_wave("batch-1", 2, "claim-2", 104)
    assert tuple(entry.owner for entry in second) == ("Gamma",)
    repository.owner_queue.finish_owner_queue_claim(
        OwnerQueueCompletion("batch-1", "3", "claim-2", "deferred", 105)
    )
    assert (
        repository.owner_queue.claim_owner_queue_wave("batch-1", 2, "claim-3", 106)
        == ()
    )

    assert repository.owner_queue.activate_paused_owner_queue("batch-1", 107) == 1
    resumed = repository.owner_queue.claim_owner_queue_wave(
        "batch-1", 2, "claim-4", 108
    )
    assert tuple(entry.owner for entry in resumed) == ("Beta",)
    completed = repository.owner_queue.owner_queue_entries(
        "batch-1", status="completed"
    )
    assert {(entry.owner, entry.status) for entry in completed} == {
        ("Alpha", "completed"),
        ("Gamma", "completed"),
    }


def test_startup_recovers_claims_and_removes_stale_generations(tmp_path: Path) -> None:
    """A killed sole writer resumes its claim under only the active generation."""

    repository = _repository(tmp_path)
    repository.owner_queue.prepare_owner_queue("batch-1", ("1/Alpha",), 100)
    repository.owner_queue.claim_owner_queue_wave("batch-1", 1, "abandoned", 101)

    recovered = repository.owner_queue.prepare_owner_queue("batch-1", ("2/Beta",), 102)
    assert len(recovered) == 1
    assert recovered[0].owner == "Alpha"
    assert recovered[0].status == "ready"
    assert recovered[0].claim_token == ""

    assert repository.owner_queue.prepare_owner_queue("batch-2", (), 103) == ()
    assert repository.owner_queue.owner_queue_entries("batch-1") == ()


def test_retryable_and_promoted_completed_work_can_reactivate(tmp_path: Path) -> None:
    """Deferrals and stronger explicit reasons reopen without changing sequence."""

    repository = _repository(tmp_path)
    repository.owner_queue.prepare_owner_queue("batch-1", (), 100)
    repository.owner_queue.admit_owner_queue(
        "batch-1",
        (OwnerQueueAdmission("1", "Alpha", "connection"),),
        101,
    )
    claimed = repository.owner_queue.claim_owner_queue_wave(
        "batch-1", 1, "claim-1", 102
    )
    repository.owner_queue.finish_owner_queue_claim(
        OwnerQueueCompletion("batch-1", "1", "claim-1", "deferred", 103)
    )

    retried = repository.owner_queue.admit_owner_queue(
        "batch-1",
        (OwnerQueueAdmission("1", "Alpha", "connection"),),
        104,
    )
    assert len(retried) == 1
    assert retried[0].sequence == claimed[0].sequence
    repository.owner_queue.claim_owner_queue_wave("batch-1", 1, "claim-2", 105)
    repository.owner_queue.finish_owner_queue_claim(
        OwnerQueueCompletion("batch-1", "1", "claim-2", "updated", 106)
    )

    assert (
        repository.owner_queue.admit_owner_queue(
            "batch-1",
            (OwnerQueueAdmission("1", "Alpha", "connection"),),
            107,
        )
        == ()
    )
    promoted = repository.owner_queue.admit_owner_queue(
        "batch-1",
        (OwnerQueueAdmission("1", "Alpha", "manual"),),
        108,
    )
    assert len(promoted) == 1
    assert promoted[0].reason == "manual"
    assert promoted[0].sequence == claimed[0].sequence
