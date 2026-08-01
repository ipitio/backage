"""Repository methods for the durable owner-work queue."""

from __future__ import annotations

import sqlite3
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

from . import owner_queue
from .owner_queue import (
    OwnerQueueAdmission,
    OwnerQueueCandidate,
    OwnerQueueCompletion,
    OwnerQueueEntry,
    OwnerQueueStats,
)


class OwnerQueueRepositoryMixin(ABC):
    """Add generation-scoped queue operations to the shared repository."""

    @abstractmethod
    def ensure_schema(self) -> None:
        """Create or migrate the lazy normalized schema."""

        raise NotImplementedError

    @abstractmethod
    def _run_read(self, operation: Callable[[sqlite3.Connection], Any]) -> Any:
        raise NotImplementedError

    @abstractmethod
    def _run_write(self, operation: Callable[[sqlite3.Connection], Any]) -> Any:
        raise NotImplementedError

    def prepare_owner_queue(
        self,
        generation: str,
        legacy_refs: tuple[str, ...],
        now: int,
    ) -> tuple[OwnerQueueEntry, ...]:
        """Recover one active generation and conditionally import legacy state."""

        self.ensure_schema()
        return self._run_write(
            lambda connection: owner_queue.prepare_generation(
                connection, generation, legacy_refs, now
            )
        )

    def admit_owner_queue(
        self,
        generation: str,
        admissions: tuple[OwnerQueueAdmission, ...],
        now: int,
    ) -> tuple[OwnerQueueEntry, ...]:
        """Add or promote owner work for the active generation."""

        self.ensure_schema()
        return self._run_write(
            lambda connection: owner_queue.admit(
                connection, generation, admissions, now
            )
        )

    def known_owner_queue_candidates(
        self,
        generation: str,
        candidates: tuple[str, ...],
    ) -> frozenset[str]:
        """Return bounded candidate logins already attempted this generation."""

        self.ensure_schema()
        return self._run_read(
            lambda connection: owner_queue.known_candidates(
                connection,
                generation,
                candidates,
            )
        )

    def record_owner_queue_candidates(
        self,
        generation: str,
        candidates: tuple[OwnerQueueCandidate, ...],
        admissions: tuple[OwnerQueueAdmission, ...],
        now: int,
    ) -> tuple[OwnerQueueEntry, ...]:
        """Record bounded candidate attempts and canonical admissions together."""

        self.ensure_schema()
        return self._run_write(
            lambda connection: owner_queue.record_candidates(
                connection,
                generation,
                candidates,
                admissions,
                now,
            )
        )

    def owner_queue_entries(
        self,
        generation: str,
        *,
        status: str | None = None,
    ) -> tuple[OwnerQueueEntry, ...]:
        """Read remaining queue entries in deterministic order."""

        self.ensure_schema()
        return self._run_read(
            lambda connection: owner_queue.entries(
                connection, generation, status=status
            )
        )

    def owner_queue_stats(self, generation: str) -> OwnerQueueStats:
        """Return compact active and stale generation telemetry."""

        self.ensure_schema()
        return self._run_read(
            lambda connection: owner_queue.stats(connection, generation)
        )

    def claim_owner_queue_wave(
        self,
        generation: str,
        limit: int,
        claim_token: str,
        now: int,
    ) -> tuple[OwnerQueueEntry, ...]:
        """Claim the next bounded ready wave."""

        self.ensure_schema()
        return self._run_write(
            lambda connection: owner_queue.claim_wave(
                connection, generation, limit, claim_token, now
            )
        )

    def finish_owner_queue_claim(
        self,
        completion: OwnerQueueCompletion,
    ) -> None:
        """Persist one parent-applied worker outcome."""

        self.ensure_schema()
        self._run_write(
            lambda connection: owner_queue.finish_claim(
                connection,
                completion,
            )
        )

    def activate_paused_owner_queue(self, generation: str, now: int) -> int:
        """Make paused rows ready for another continuation pass."""

        self.ensure_schema()
        return self._run_write(
            lambda connection: owner_queue.activate_paused(connection, generation, now)
        )
