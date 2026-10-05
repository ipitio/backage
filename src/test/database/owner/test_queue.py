"""Tests for the lazy generation-scoped SQLite owner queue."""

import sqlite3
from pathlib import Path

import pytest

from bkg_py.database.composition import DatabaseRepositories
from bkg_py.database.models import OwnerScanFailure
from bkg_py.database.owner.queue import (
    OwnerQueueAdmission,
    OwnerQueueCandidate,
    OwnerQueueCompletion,
    OwnerQueueOutcome,
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


@pytest.mark.parametrize("outcome", ["deferred", "updated"])
def test_startup_recovers_due_failed_owners_despite_candidate_deduplication(
    tmp_path: Path, outcome: OwnerQueueOutcome
) -> None:
    """Published partial work and failed listings both resume after backoff."""

    repository = _repository(tmp_path)
    repository.owner_queue.record_owner_queue_candidates(
        "batch-1",
        (OwnerQueueCandidate("Alpha", "connection"),),
        (
            OwnerQueueAdmission("1", "Alpha", "connection"),
            OwnerQueueAdmission("2", "Beta", "connection"),
        ),
        100,
    )
    repository.owner_queue.claim_owner_queue_wave("batch-1", 2, "claim-1", 101)
    for owner_id in ("1", "2"):
        repository.owner_queue.finish_owner_queue_claim(
            OwnerQueueCompletion(
                "batch-1",
                owner_id,
                "claim-1",
                outcome if owner_id == "1" else "updated",
                102,
            )
        )
    repository.owners.begin_owner_scan("1", "Alpha", "batch-1", 100)
    retry_after = repository.owners.fail_owner_scan(
        OwnerScanFailure("1", "Alpha", "batch-1", "inventory unavailable", 102)
    )

    restarted = _repository(tmp_path)
    assert (
        restarted.owner_queue.prepare_owner_queue("batch-1", (), retry_after - 1) == ()
    )
    resumed = restarted.owner_queue.prepare_owner_queue("batch-1", (), retry_after)

    assert tuple(entry.ref for entry in resumed) == ("1/Alpha",)
    assert resumed[0].sequence == 0
    assert resumed[0].status == "ready"
    assert restarted.owner_queue.known_owner_queue_candidates(
        "batch-1", ("alpha",)
    ) == frozenset({"alpha"})
    assert (
        restarted.owner_queue.prepare_owner_queue("batch-1", (), retry_after + 1)
        == resumed
    )
    restarted.owner_queue.claim_owner_queue_wave(
        "batch-1", 2, "claim-2", retry_after + 2
    )
    restarted.owner_queue.finish_owner_queue_claim(
        OwnerQueueCompletion("batch-1", "1", "claim-2", "updated", retry_after + 3)
    )
    restarted.owners.clear_owner_backoff("1", "Alpha", retry_after + 3)
    assert (
        restarted.owner_queue.prepare_owner_queue("batch-1", (), retry_after + 4) == ()
    )


@pytest.mark.parametrize("status", ["ready", "claimed", "paused"])
def test_recovered_queue_rows_respect_persisted_owner_cooldown(
    tmp_path: Path, status: str
) -> None:
    """A stale queue row cannot retry a failed owner before its saved deadline."""

    repository = _repository(tmp_path)
    repository.owner_queue.admit_owner_queue(
        "batch-1",
        (
            OwnerQueueAdmission("1", "Alpha", "connection"),
            OwnerQueueAdmission("2", "Beta", "connection"),
        ),
        100,
    )
    if status != "ready":
        repository.owner_queue.claim_owner_queue_wave("batch-1", 1, "old-claim", 101)
    if status == "paused":
        repository.owner_queue.finish_owner_queue_claim(
            OwnerQueueCompletion("batch-1", "1", "old-claim", "paused", 102)
        )
    repository.owners.begin_owner_scan("1", "Alpha", "batch-1", 100)
    retry_after = repository.owners.fail_owner_scan(
        OwnerScanFailure("1", "Alpha", "batch-1", "inventory unavailable", 102)
    )

    restarted = _repository(tmp_path)
    remaining = restarted.owner_queue.prepare_owner_queue("batch-1", (), 103)
    assert tuple(entry.owner for entry in remaining) == ("Beta",)
    claimed = restarted.owner_queue.claim_owner_queue_wave(
        "batch-1", 2, "new-claim", 104
    )
    assert tuple(entry.owner for entry in claimed) == ("Beta",)
    restarted.owner_queue.finish_owner_queue_claim(
        OwnerQueueCompletion("batch-1", "2", "new-claim", "updated", 105)
    )
    assert restarted.owner_queue.activate_paused_owner_queue("batch-1", 106) == 0
    assert restarted.owner_queue.owner_queue_entries("batch-1") == ()
    assert (
        restarted.owner_queue.prepare_owner_queue("batch-1", (), retry_after - 1) == ()
    )
    resumed = restarted.owner_queue.prepare_owner_queue("batch-1", (), retry_after)
    assert tuple(entry.owner for entry in resumed) == ("Alpha",)
    assert resumed[0].sequence == 0


@pytest.mark.parametrize("previous", ["absent", "deferred", "ready"])
def test_automatic_admission_cannot_bypass_owner_cooldown(
    tmp_path: Path, previous: str
) -> None:
    """Fresh rows, retries, and stronger automatic priorities all honor backoff."""

    repository = _repository(tmp_path)
    repository.owners.begin_owner_scan("1", "Alpha", "batch-1", 100)
    if previous != "absent":
        repository.owner_queue.admit_owner_queue(
            "batch-1", (OwnerQueueAdmission("1", "Alpha", "connection"),), 100
        )
    if previous == "deferred":
        repository.owner_queue.claim_owner_queue_wave("batch-1", 1, "old-claim", 101)
        repository.owner_queue.finish_owner_queue_claim(
            OwnerQueueCompletion("batch-1", "1", "old-claim", "deferred", 102)
        )
    retry_after = repository.owners.fail_owner_scan(
        OwnerScanFailure("1", "Alpha", "batch-1", "inventory unavailable", 102)
    )

    repository.owner_queue.record_owner_queue_candidates(
        "batch-1",
        (OwnerQueueCandidate("Alpha", "partially-updated"),),
        (OwnerQueueAdmission("1", "Alpha", "partially-updated"),),
        103,
    )

    assert repository.owner_queue.owner_queue_entries("batch-1") == ()
    assert (
        repository.owner_queue.claim_owner_queue_wave("batch-1", 1, "claim", 104) == ()
    )
    assert repository.owner_queue.known_owner_queue_candidates(
        "batch-1", ("Alpha",)
    ) == frozenset({"alpha"})
    resumed = repository.owner_queue.prepare_owner_queue("batch-1", (), retry_after)
    assert tuple(entry.owner for entry in resumed) == ("Alpha",)
    assert resumed[0].reason == "partially-updated"


@pytest.mark.parametrize("reason", ["manual", "optout"])
def test_explicit_admission_overrides_backoff_only_until_restart(
    tmp_path: Path, reason: str
) -> None:
    """An explicit override admits work but does not survive as a stale claim."""

    repository = _repository(tmp_path)
    repository.owners.begin_owner_scan("1", "Alpha", "batch-1", 100)
    repository.owners.fail_owner_scan(
        OwnerScanFailure("1", "Alpha", "batch-1", "inventory unavailable", 102)
    )
    repository.owner_queue.admit_owner_queue(
        "batch-1", (OwnerQueueAdmission("1", "Alpha", "connection"),), 103
    )

    repository.owner_queue.admit_owner_queue(
        "batch-1", (OwnerQueueAdmission("1", "Alpha", reason),), 104
    )
    claimed = repository.owner_queue.claim_owner_queue_wave("batch-1", 1, "claim", 105)
    assert tuple(entry.owner for entry in claimed) == ("Alpha",)
    assert claimed[0].reason == reason

    restarted = _repository(tmp_path)
    assert restarted.owner_queue.prepare_owner_queue("batch-1", (), 106) == ()
    assert (
        restarted.owner_queue.claim_owner_queue_wave("batch-1", 1, "retry", 107) == ()
    )
    restarted.owner_queue.admit_owner_queue(
        "batch-1", (OwnerQueueAdmission("1", "Alpha", reason),), 108
    )
    assert (
        len(restarted.owner_queue.claim_owner_queue_wave("batch-1", 1, "fresh", 109))
        == 1
    )


def test_automatic_admission_preserves_an_active_claim(tmp_path: Path) -> None:
    """A failure saved by a worker does not let admission steal its parent claim."""

    repository = _repository(tmp_path)
    repository.owner_queue.admit_owner_queue(
        "batch-1", (OwnerQueueAdmission("1", "Alpha", "connection"),), 100
    )
    claimed = repository.owner_queue.claim_owner_queue_wave("batch-1", 1, "claim", 101)
    repository.owners.begin_owner_scan("1", "Alpha", "batch-1", 101)
    repository.owners.fail_owner_scan(
        OwnerScanFailure("1", "Alpha", "batch-1", "inventory unavailable", 102)
    )

    assert (
        repository.owner_queue.admit_owner_queue(
            "batch-1", (OwnerQueueAdmission("1", "Alpha", "partially-updated"),), 103
        )
        == ()
    )
    remaining = repository.owner_queue.owner_queue_entries("batch-1")
    assert len(remaining) == 1
    assert remaining[0].status == "claimed"
    assert remaining[0].claim_token == claimed[0].claim_token
    assert remaining[0].claimed_at == claimed[0].claimed_at
    repository.owner_queue.finish_owner_queue_claim(
        OwnerQueueCompletion("batch-1", "1", "claim", "deferred", 104)
    )
    assert repository.owner_queue.prepare_owner_queue("batch-1", (), 105) == ()
