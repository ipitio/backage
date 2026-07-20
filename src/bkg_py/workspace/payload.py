"""Import workflow-downloaded files into a checked-out repository."""

from __future__ import annotations

import shutil
from pathlib import Path

from .repository import WorkspaceError


def import_workflow_payload(payload_dir: Path, destination: Path) -> None:
    """Merge downloaded workflow payload entries into a repository root."""

    if not payload_dir.is_dir():
        return
    destination.mkdir(parents=True, exist_ok=True)
    for entry in sorted(payload_dir.iterdir(), key=lambda path: path.name):
        _move_entry(entry, destination / entry.name)


def _move_entry(source: Path, target: Path) -> None:
    source_is_dir = source.is_dir() and not source.is_symlink()
    target_is_dir = target.is_dir() and not target.is_symlink()

    if source_is_dir and target_is_dir:
        for entry in sorted(source.iterdir(), key=lambda path: path.name):
            _move_entry(entry, target / entry.name)
        source.rmdir()
        return

    target_exists = target.exists() or target.is_symlink()
    if target_exists and (source_is_dir or target_is_dir):
        raise WorkspaceError(
            f"cannot replace {target} with differently shaped payload entry"
        )

    try:
        shutil.move(str(source), str(target))
    except (OSError, shutil.Error) as error:
        raise WorkspaceError(
            f"failed to import workflow payload entry {source}: {error}"
        ) from error
