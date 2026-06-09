"""Atomic file-writing helpers for durable bkg outputs."""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import stat
import tempfile
from typing import BinaryIO, Generator, TextIO


def _destination_mode(path: Path, default_mode: int) -> int:
    try:
        return stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        return default_mode


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def atomic_path(
    destination: Path,
    *,
    default_mode: int = 0o644,
) -> Generator[Path, None, None]:
    """Yield a sibling path and replace the destination after successful use."""

    descriptor, temporary_name = tempfile.mkstemp(
        dir=destination.parent,
        prefix=f".{destination.name}.",
    )
    temporary_path = Path(temporary_name)
    os.close(descriptor)

    try:
        os.chmod(temporary_path, _destination_mode(destination, default_mode))
        yield temporary_path
        with temporary_path.open("rb") as file:
            os.fsync(file.fileno())
        os.replace(temporary_path, destination)
        _sync_directory(destination.parent)
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


@contextmanager
def atomic_text_output(
    destination: Path,
    *,
    encoding: str = "utf-8",
    newline: str | None = "\n",
    default_mode: int = 0o644,
) -> Generator[TextIO, None, None]:
    """Yield a text stream that atomically replaces its destination."""

    with atomic_path(destination, default_mode=default_mode) as temporary_path:
        with temporary_path.open(
            "w",
            encoding=encoding,
            newline=newline,
        ) as file:
            yield file
            file.flush()
            os.fsync(file.fileno())


@contextmanager
def atomic_binary_output(
    destination: Path,
    *,
    default_mode: int = 0o644,
) -> Generator[BinaryIO, None, None]:
    """Yield a binary stream that atomically replaces its destination."""

    with atomic_path(destination, default_mode=default_mode) as temporary_path:
        with temporary_path.open("wb") as file:
            yield file
            file.flush()
            os.fsync(file.fileno())
