"""Repository methods for durable database-rotation events."""

from __future__ import annotations

import sqlite3
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

from . import rotation_events
from .models import DatabaseRotationEvent


class DatabaseRotationRepositoryMixin(ABC):
    """Add rotation event writes and release-scoped reads to the repository."""

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

    def record_database_rotation(self, event: DatabaseRotationEvent) -> None:
        """Persist one completed database rotation."""

        self.ensure_schema()
        self._run_write(lambda connection: rotation_events.record(connection, event))

    def database_rotations_for_release(
        self,
        release_tag: str,
    ) -> tuple[DatabaseRotationEvent, ...]:
        """Return durable rotation events for one release."""

        self.ensure_schema()
        return self._run_read(
            lambda connection: rotation_events.for_release(connection, release_tag)
        )
