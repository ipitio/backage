"""Durable database-rotation event storage."""

from __future__ import annotations

import sqlite3

from .models import DatabaseRotationEvent

_RECORD_SQL = """
    insert into "bkg_rotation_events" (
        release_tag, rotated_at, archive_name, source_bytes,
        compressed_bytes, retained_since
    ) values (?, ?, ?, ?, ?, ?)
    on conflict(archive_name) do update set
        release_tag = excluded.release_tag,
        rotated_at = excluded.rotated_at,
        source_bytes = excluded.source_bytes,
        compressed_bytes = excluded.compressed_bytes,
        retained_since = excluded.retained_since
"""
_FOR_RELEASE_SQL = """
    select release_tag, rotated_at, archive_name, source_bytes,
           compressed_bytes, retained_since
    from "bkg_rotation_events"
    where release_tag = ?
    order by rotated_at, event_id
"""


def record(connection: sqlite3.Connection, event: DatabaseRotationEvent) -> None:
    """Persist one rotation event idempotently by archive name."""

    connection.execute(
        _RECORD_SQL,
        (
            event.release_tag,
            event.rotated_at,
            event.archive_name,
            event.source_bytes,
            event.compressed_bytes,
            event.retained_since,
        ),
    )


def for_release(
    connection: sqlite3.Connection,
    release_tag: str,
) -> tuple[DatabaseRotationEvent, ...]:
    """Return all rotation events for one release in occurrence order."""

    rows = connection.execute(_FOR_RELEASE_SQL, (release_tag,)).fetchall()
    return tuple(
        DatabaseRotationEvent(
            release_tag=str(row[0]),
            rotated_at=str(row[1]),
            archive_name=str(row[2]),
            source_bytes=int(row[3]),
            compressed_bytes=int(row[4]),
            retained_since=str(row[5]),
        )
        for row in rows
    )
