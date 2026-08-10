"""Durable generation-scoped owner work admission and claiming."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Literal

from .support import DatabaseError

OwnerQueueOutcome = Literal["updated", "paused", "missing", "deferred", "opted-out"]
_OWNER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}")
_PRIORITIES = {
    "manual": 0,
    "optout": 0,
    "partially-updated": 10,
    "connection": 15,
    "stale": 20,
    "service-owner": 30,
    "targeted": 30,
    "discovered": 50,
    "legacy": 50,
    "index-history": 60,
}


@dataclass(frozen=True)
class OwnerQueueAdmission:
    """One canonical owner proposed for an active queue generation."""

    owner_id: str
    owner: str
    reason: str

    @property
    def ref(self) -> str:
        """Return the serialized owner reference."""

        return f"{self.owner_id}/{self.owner}"


@dataclass(frozen=True)
class OwnerQueueCandidate:
    """One login attempted while filling an active queue generation."""

    owner: str
    reason: str


@dataclass(frozen=True)
class OwnerQueueStats:  # pylint: disable=too-many-instance-attributes
    """Compact queue and candidate counts for one active generation."""

    total: int
    ready: int
    claimed: int
    paused: int
    completed: int
    candidates: int
    stale_rows: int
    stale_candidates: int


@dataclass(frozen=True)
class OwnerQueueEntry:  # pylint: disable=too-many-instance-attributes
    """One ordered durable owner-work row."""

    owner_id: str
    owner: str
    reason: str
    priority: int
    sequence: int
    status: str
    claim_token: str = ""
    claimed_at: int = 0

    @property
    def ref(self) -> str:
        """Return the serialized owner reference."""

        return f"{self.owner_id}/{self.owner}"


@dataclass(frozen=True)
class OwnerQueueCompletion:
    """One worker outcome applied by the parent process."""

    generation: str
    owner_id: str
    claim_token: str
    outcome: OwnerQueueOutcome
    finished_at: int


def prepare_generation(
    connection: sqlite3.Connection,
    generation: str,
    legacy_refs: tuple[str, ...],
    now: int,
) -> tuple[OwnerQueueEntry, ...]:
    """Recover one generation and import legacy state only when it is empty."""

    _validate_generation(generation)
    _validate_time(now)
    with _transaction(connection):
        connection.execute(
            'delete from "bkg_owner_queue" where generation != ?',
            (generation,),
        )
        connection.execute(
            'delete from "bkg_owner_queue_candidates" where generation != ?',
            (generation,),
        )
        _normalize_priorities(connection, generation)
        connection.execute(
            """
            update "bkg_owner_queue"
            set status = 'ready', claim_token = '', claimed_at = 0,
                updated_at = ?
            where generation = ? and status = 'claimed'
            """,
            (now, generation),
        )
        row = connection.execute(
            'select count(*) from "bkg_owner_queue" where generation = ?',
            (generation,),
        ).fetchone()
        if row is None:
            raise DatabaseError("owner queue count returned no row")
        if int(row[0]) == 0:
            admissions = tuple(_legacy_admission(value) for value in legacy_refs)
            _admit(connection, generation, admissions, now)
    return entries(connection, generation)


def _normalize_priorities(connection: sqlite3.Connection, generation: str) -> None:
    """Apply current reason priorities to rows retained across an upgrade."""

    for reason, priority in _PRIORITIES.items():
        connection.execute(
            """
            update "bkg_owner_queue"
            set priority = ?
            where generation = ? and reason = ? and priority != ?
            """,
            (priority, generation, reason, priority),
        )


def admit(
    connection: sqlite3.Connection,
    generation: str,
    admissions: tuple[OwnerQueueAdmission, ...],
    now: int,
) -> tuple[OwnerQueueEntry, ...]:
    """Insert or promote canonical owners without destabilizing queue order."""

    _validate_generation(generation)
    _validate_time(now)
    normalized = tuple(_validated_admission(item) for item in admissions)
    with _transaction(connection):
        return _admit(connection, generation, normalized, now)


def known_candidates(
    connection: sqlite3.Connection,
    generation: str,
    candidates: tuple[str, ...],
) -> frozenset[str]:
    """Return candidate logins already attempted this generation."""

    _validate_generation(generation)
    owner_keys = tuple(dict.fromkeys(_candidate_key(value) for value in candidates))
    known: set[str] = set()
    for offset in range(0, len(owner_keys), 400):
        chunk = owner_keys[offset : offset + 400]
        rows = connection.execute(
            """
            select owner_key from "bkg_owner_queue_candidates"
            where generation = ?
              and owner_key in (select value from json_each(?))
            """,
            (generation, json.dumps(chunk)),
        ).fetchall()
        known.update(str(row[0]) for row in rows)
    return frozenset(known)


def record_candidates(
    connection: sqlite3.Connection,
    generation: str,
    candidates: tuple[OwnerQueueCandidate, ...],
    admissions: tuple[OwnerQueueAdmission, ...],
    now: int,
) -> tuple[OwnerQueueEntry, ...]:
    """Record attempted logins and canonical admissions atomically."""

    _validate_generation(generation)
    _validate_time(now)
    normalized_candidates = tuple(
        _validated_candidate(candidate) for candidate in candidates
    )
    normalized_admissions = tuple(
        _validated_admission(admission) for admission in admissions
    )
    with _transaction(connection):
        for candidate in normalized_candidates:
            connection.execute(
                """
                insert into "bkg_owner_queue_candidates" (
                    generation, owner, owner_key, reason, attempted_at
                ) values (?, ?, ?, ?, ?)
                on conflict (generation, owner_key) do nothing
                """,
                (
                    generation,
                    candidate.owner,
                    candidate.owner.casefold(),
                    candidate.reason,
                    now,
                ),
            )
        return _admit(connection, generation, normalized_admissions, now)


def entries(
    connection: sqlite3.Connection,
    generation: str,
    *,
    status: str | None = None,
) -> tuple[OwnerQueueEntry, ...]:
    """Return remaining rows in deterministic priority and admission order."""

    _validate_generation(generation)
    if status is None:
        rows = connection.execute(
            """
            select owner_id, owner, reason, priority, sequence, status,
                   claim_token, claimed_at
            from "bkg_owner_queue"
            where generation = ? and status != 'completed'
            order by priority, sequence
            """,
            (generation,),
        ).fetchall()
    else:
        _validate_status(status)
        rows = connection.execute(
            """
            select owner_id, owner, reason, priority, sequence, status,
                   claim_token, claimed_at
            from "bkg_owner_queue"
            where generation = ? and status = ?
            order by priority, sequence
            """,
            (generation, status),
        ).fetchall()
    return tuple(_entry(row) for row in rows)


def stats(connection: sqlite3.Connection, generation: str) -> OwnerQueueStats:
    """Return active-generation and stale-generation queue counts."""

    _validate_generation(generation)
    row = connection.execute(
        """
        select
            count(*),
            coalesce(sum(status = 'ready'), 0),
            coalesce(sum(status = 'claimed'), 0),
            coalesce(sum(status = 'paused'), 0),
            coalesce(sum(status = 'completed'), 0),
            (select count(*) from "bkg_owner_queue_candidates"
             where generation = ?),
            (select count(*) from "bkg_owner_queue" where generation != ?),
            (select count(*) from "bkg_owner_queue_candidates"
             where generation != ?)
        from "bkg_owner_queue"
        where generation = ?
        """,
        (generation, generation, generation, generation),
    ).fetchone()
    if row is None:
        raise DatabaseError("owner queue stats returned no row")
    return OwnerQueueStats(*(int(value) for value in row))


def claim_wave(
    connection: sqlite3.Connection,
    generation: str,
    limit: int,
    claim_token: str,
    now: int,
) -> tuple[OwnerQueueEntry, ...]:
    """Claim the next bounded ready wave for the sole serialized writer."""

    _validate_generation(generation)
    _validate_time(now)
    if limit < 1:
        raise DatabaseError("owner queue claim limit must be positive")
    if not claim_token:
        raise DatabaseError("owner queue claim token is required")
    with _transaction(connection):
        rows = connection.execute(
            """
            select owner_id
            from "bkg_owner_queue"
            where generation = ? and status = 'ready' and attempt_after <= ?
            order by priority, sequence
            limit ?
            """,
            (generation, now, limit),
        ).fetchall()
        owner_ids = tuple(str(row[0]) for row in rows)
        if owner_ids:
            for owner_id in owner_ids:
                connection.execute(
                    """
                update "bkg_owner_queue"
                set status = 'claimed', claim_token = ?, claimed_at = ?,
                    updated_at = ?
                where generation = ? and owner_id = ? and status = 'ready'
                    """,
                    (claim_token, now, now, generation, owner_id),
                )
        return _claimed_entries(connection, generation, claim_token)


def finish_claim(
    connection: sqlite3.Connection,
    completion: OwnerQueueCompletion,
) -> None:
    """Commit one parent-applied worker outcome to its durable queue row."""

    _validate_generation(completion.generation)
    _validate_time(completion.finished_at)
    if not completion.owner_id or not completion.claim_token:
        raise DatabaseError("owner queue completion requires identity and claim token")
    with _transaction(connection):
        row = connection.execute(
            """
            select status, claim_token from "bkg_owner_queue"
            where generation = ? and owner_id = ?
            """,
            (completion.generation, completion.owner_id),
        ).fetchone()
        if row is None:
            return
        if str(row[0]) != "claimed" or str(row[1]) != completion.claim_token:
            raise DatabaseError("owner queue completion does not own the active claim")
        if completion.outcome == "paused":
            connection.execute(
                """
                update "bkg_owner_queue"
                set status = 'paused', claim_token = '', claimed_at = 0,
                    updated_at = ?
                where generation = ? and owner_id = ?
                """,
                (
                    completion.finished_at,
                    completion.generation,
                    completion.owner_id,
                ),
            )
        else:
            connection.execute(
                """
                update "bkg_owner_queue"
                set status = 'completed', claim_token = '', claimed_at = 0,
                    outcome = ?, finished_at = ?, updated_at = ?
                where generation = ? and owner_id = ?
                """,
                (
                    completion.outcome,
                    completion.finished_at,
                    completion.finished_at,
                    completion.generation,
                    completion.owner_id,
                ),
            )


def activate_paused(
    connection: sqlite3.Connection,
    generation: str,
    now: int,
) -> int:
    """Make every paused owner eligible for one later continuation pass."""

    _validate_generation(generation)
    _validate_time(now)
    with _transaction(connection):
        cursor = connection.execute(
            """
            update "bkg_owner_queue"
            set status = 'ready', updated_at = ?
            where generation = ? and status = 'paused'
            """,
            (now, generation),
        )
    return cursor.rowcount


def retire_owner(connection: sqlite3.Connection, owner: str) -> None:
    """Remove queue rows for an owner retired from canonical package state."""

    connection.execute(
        'delete from "bkg_owner_queue" where owner = ? collate nocase',
        (owner,),
    )


def _admit(
    connection: sqlite3.Connection,
    generation: str,
    admissions: tuple[OwnerQueueAdmission, ...],
    now: int,
) -> tuple[OwnerQueueEntry, ...]:
    next_sequence = _next_sequence(connection, generation)
    added_ids: list[str] = []
    for admission in admissions:
        priority = _PRIORITIES.get(admission.reason, _PRIORITIES["discovered"])
        owner_key = admission.owner.casefold()
        row = connection.execute(
            """
            select owner_id, priority, status, outcome
            from "bkg_owner_queue"
            where generation = ? and (owner_id = ? or owner_key = ?)
            order by owner_id = ? desc
            limit 1
            """,
            (generation, admission.owner_id, owner_key, admission.owner_id),
        ).fetchone()
        if row is None:
            connection.execute(
                """
                insert into "bkg_owner_queue" (
                    generation, owner_id, owner, owner_key, priority, sequence,
                    reason, status, attempt_after, claim_token, claimed_at,
                    created_at, updated_at
                ) values (?, ?, ?, ?, ?, ?, ?, 'ready', 0, '', 0, ?, ?)
                """,
                (
                    generation,
                    admission.owner_id,
                    admission.owner,
                    owner_key,
                    priority,
                    next_sequence,
                    admission.reason,
                    now,
                    now,
                ),
            )
            added_ids.append(admission.owner_id)
            next_sequence += 1
            continue
        persisted_id = str(row[0])
        persisted_priority = int(row[1])
        reactivated = str(row[2]) == "completed" and (
            str(row[3]) == "deferred"
            or admission.reason == "optout"
            or priority < persisted_priority
        )
        connection.execute(
            """
            update "bkg_owner_queue"
            set owner_id = ?, owner = ?, owner_key = ?,
                priority = ?, reason = ?,
                status = case when ? then 'ready' else status end,
                outcome = case when ? then '' else outcome end,
                finished_at = case when ? then 0 else finished_at end,
                updated_at = ?
            where generation = ? and owner_id = ?
            """,
            (
                admission.owner_id,
                admission.owner,
                owner_key,
                min(priority, persisted_priority),
                admission.reason
                if priority < persisted_priority
                else _persisted_reason(connection, generation, persisted_id),
                reactivated,
                reactivated,
                reactivated,
                now,
                generation,
                persisted_id,
            ),
        )
        if reactivated:
            added_ids.append(admission.owner_id)
    if not added_ids:
        return ()
    added = set(added_ids)
    return tuple(
        entry for entry in entries(connection, generation) if entry.owner_id in added
    )


def _claimed_entries(
    connection: sqlite3.Connection,
    generation: str,
    claim_token: str,
) -> tuple[OwnerQueueEntry, ...]:
    rows = connection.execute(
        """
        select owner_id, owner, reason, priority, sequence, status,
               claim_token, claimed_at
        from "bkg_owner_queue"
        where generation = ? and claim_token = ?
        order by priority, sequence
        """,
        (generation, claim_token),
    ).fetchall()
    return tuple(_entry(row) for row in rows)


def _persisted_reason(
    connection: sqlite3.Connection,
    generation: str,
    owner_id: str,
) -> str:
    row = connection.execute(
        'select reason from "bkg_owner_queue" where generation = ? and owner_id = ?',
        (generation, owner_id),
    ).fetchone()
    if row is None:
        raise DatabaseError("owner queue row disappeared during admission")
    return str(row[0])


def _next_sequence(connection: sqlite3.Connection, generation: str) -> int:
    row = connection.execute(
        """
        select coalesce(max(sequence), -1) + 1
        from "bkg_owner_queue" where generation = ?
        """,
        (generation,),
    ).fetchone()
    if row is None:
        raise DatabaseError("owner queue sequence returned no row")
    return int(row[0])


def _legacy_admission(value: str) -> OwnerQueueAdmission:
    owner_id, separator, owner = value.partition("/")
    if not separator:
        raise DatabaseError(f"invalid legacy owner queue reference: {value}")
    return _validated_admission(OwnerQueueAdmission(owner_id, owner, "legacy"))


def _validated_admission(admission: OwnerQueueAdmission) -> OwnerQueueAdmission:
    if not admission.owner_id.isdecimal() or int(admission.owner_id) < 0:
        raise DatabaseError(f"invalid owner queue ID: {admission.owner_id}")
    if _OWNER_PATTERN.fullmatch(admission.owner) is None:
        raise DatabaseError(f"invalid owner queue login: {admission.owner}")
    if not admission.reason:
        raise DatabaseError("owner queue admission reason is required")
    return admission


def _validated_candidate(candidate: OwnerQueueCandidate) -> OwnerQueueCandidate:
    owner = _candidate_owner(candidate.owner)
    if not candidate.reason:
        raise DatabaseError("owner queue candidate reason is required")
    return OwnerQueueCandidate(owner, candidate.reason)


def _candidate_key(value: str) -> str:
    return _candidate_owner(value).casefold()


def _candidate_owner(value: str) -> str:
    owner = value.split("/", maxsplit=1)[-1]
    if _OWNER_PATTERN.fullmatch(owner) is None:
        raise DatabaseError(f"invalid owner queue candidate: {value}")
    return owner


def _entry(row: sqlite3.Row | tuple[object, ...]) -> OwnerQueueEntry:
    return OwnerQueueEntry(
        owner_id=str(row[0]),
        owner=str(row[1]),
        reason=str(row[2]),
        priority=int(str(row[3])),
        sequence=int(str(row[4])),
        status=str(row[5]),
        claim_token=str(row[6]),
        claimed_at=int(str(row[7])),
    )


def _validate_generation(generation: str) -> None:
    if not generation:
        raise DatabaseError("owner queue generation is required")


def _validate_time(now: int) -> None:
    if now < 0:
        raise DatabaseError("owner queue time cannot be negative")


def _validate_status(status: str) -> None:
    if status not in {"ready", "claimed", "paused", "completed"}:
        raise DatabaseError(f"invalid owner queue status: {status}")


@contextmanager
def _transaction(connection: sqlite3.Connection) -> Generator[None]:
    connection.execute("begin immediate")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    connection.commit()
