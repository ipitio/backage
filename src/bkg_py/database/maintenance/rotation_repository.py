"""Repository methods for durable database-rotation events."""

from ..kernel import DatabaseComponent
from ..models import DatabaseRotationEvent
from . import rotations as rotation_events


class DatabaseRotationRepository(DatabaseComponent):
    """Provide durable rotation event writes and release-scoped reads."""

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
