"""Stage endpoint pairs and restore previous files if replacement fails."""

import shutil
import stat
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from ..files import atomic_path, sync_directory

_BACKUP_PREFIX = ".bkg-pair-"
_BACKUP_MARKER = ".bkg-publication-backup"
_BACKUP_NAMES = ("json.previous", "xml.previous")
_BACKUP_FILES = (
    _BACKUP_MARKER,
    *_BACKUP_NAMES,
    *(Path(name).with_suffix(".restore").name for name in _BACKUP_NAMES),
)


class PublicationRecoveryError(RuntimeError):
    """Output restoration or backup cleanup failed; publication must abort."""


@dataclass(frozen=True)
class _OutputBackup:
    """The previous endpoint inode or its recorded absence."""

    destination: Path
    previous: Path | None


def is_publication_backup(path: Path) -> bool:
    """Identify retained pair backups that must never enter the public index."""

    return (
        path.name.startswith(_BACKUP_PREFIX)
        and path.is_dir()
        and any((path / name).is_file() for name in _BACKUP_FILES)
    )


def _backup_output(destination: Path, previous: Path) -> _OutputBackup:
    try:
        mode = destination.lstat().st_mode
    except FileNotFoundError:
        return _OutputBackup(destination, None)
    if not stat.S_ISREG(mode):
        raise OSError(f"publication destination is not a regular file: {destination}")
    previous.hardlink_to(destination)
    return _OutputBackup(destination, previous)


def _restore_outputs(backups: tuple[_OutputBackup, ...]) -> None:
    changed = False
    for output in backups:
        if output.previous is None:
            try:
                output.destination.unlink()
            except FileNotFoundError:
                continue
        else:
            try:
                unchanged = output.destination.samefile(output.previous)
            except FileNotFoundError:
                unchanged = False
            if unchanged:
                continue
            replacement = output.previous.with_suffix(".restore")
            replacement.hardlink_to(output.previous)
            replacement.replace(output.destination)
        changed = True
    if changed:
        sync_directory(backups[0].destination.parent)


@contextmanager
def staged_output_pair(json_path: Path, xml_path: Path) -> Generator[tuple[Path, Path]]:
    """Stage sibling files, then replace both or restore their previous state."""

    if json_path == xml_path or json_path.parent != xml_path.parent:
        raise ValueError("paired publication requires distinct sibling destinations")
    directory = Path(tempfile.mkdtemp(dir=json_path.parent, prefix=_BACKUP_PREFIX))
    retain_backups = False
    try:
        (directory / _BACKUP_MARKER).touch(exist_ok=False)
        backups = tuple(
            _backup_output(path, directory / name)
            for path, name in zip((json_path, xml_path), _BACKUP_NAMES, strict=True)
        )
        try:
            with (
                atomic_path(xml_path) as temporary_xml,
                atomic_path(json_path) as temporary_json,
            ):
                yield temporary_json, temporary_xml
        except BaseException as publication_error:
            try:
                _restore_outputs(backups)
            except OSError as recovery_error:
                retain_backups = True
                raise PublicationRecoveryError(
                    f"publication failed ({publication_error}); restoration failed: "
                    f"{recovery_error}; previous outputs retained at {directory}"
                ) from recovery_error
            raise
    finally:
        if not retain_backups:
            try:
                shutil.rmtree(directory)
            except OSError as error:
                raise PublicationRecoveryError(
                    f"failed to remove publication backups at {directory}: {error}"
                ) from error
