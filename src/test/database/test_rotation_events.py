"""Tests for durable release-scoped database-rotation events."""

from __future__ import annotations

from pathlib import Path

from bkg_py.database.composition import DatabaseRepositories
from bkg_py.database.models import DatabaseRotationEvent
from bkg_py.database.settings import DatabaseSettings


def test_rotation_events_preserve_release_history_across_rotation_cleanup(
    tmp_path: Path,
) -> None:
    """Multiple same-day events survive pruning and remain release-scoped."""

    repository = DatabaseRepositories(DatabaseSettings(tmp_path / "index.db"))
    first = DatabaseRotationEvent(
        "v2026.7.0",
        "2026-07-05T01:02:03.000004Z",
        "2026.07.05T01.02.03.000004Z.index.db.zst",
        200,
        75,
        "2026-06-12",
    )
    second = DatabaseRotationEvent(
        "v2026.7.0",
        "2026-07-05T05:06:07.000008Z",
        "2026.07.05T05.06.07.000008Z.index.db.zst",
        220,
        80,
        "2026-06-26",
    )
    next_release = DatabaseRotationEvent(
        "v2026.7.1",
        "2026-07-16T01:02:03.000004Z",
        "2026.07.16T01.02.03.000004Z.index.db.zst",
        230,
        85,
        "2026-07-10",
    )
    for event in (second, next_release, first):
        repository.rotations.record_database_rotation(event)

    repository.packages.cleanup_replaced_legacy_tables(
        since="2026-07-01",
        prune_normalized=True,
    )

    assert repository.rotations.database_rotations_for_release("v2026.7.0") == (
        first,
        second,
    )
    assert repository.rotations.database_rotations_for_release("v2026.7.1") == (
        next_release,
    )


def test_rotation_event_record_is_idempotent_by_archive_name(tmp_path: Path) -> None:
    """Retrying one archive event updates its measurements without duplication."""

    repository = DatabaseRepositories(DatabaseSettings(tmp_path / "index.db"))
    event = DatabaseRotationEvent(
        "v2026.7.0",
        "2026-07-05T01:02:03.000004Z",
        "2026.07.05T01.02.03.000004Z.index.db.zst",
        200,
        75,
        "2026-06-12",
    )
    repository.rotations.record_database_rotation(event)
    corrected = DatabaseRotationEvent(
        event.release_tag,
        event.rotated_at,
        event.archive_name,
        event.source_bytes,
        76,
        event.retained_since,
    )

    repository.rotations.record_database_rotation(corrected)

    assert repository.rotations.database_rotations_for_release(event.release_tag) == (
        corrected,
    )
