"""Stop-aware advisory file locking for shared runtime files."""

from __future__ import annotations

import fcntl
import os
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

LockWaitCheck = Callable[[Path, float], None]
LockDiagnostic = Callable[[str], None]


class FileLockTimeout(TimeoutError):
    """A live lock holder did not release a file within the bounded wait."""


@dataclass(frozen=True)
class FileLockOptions:
    """Acquisition policy and runtime hooks for one advisory lock."""

    poll_interval: float = 0.05
    timeout: float = 30.0
    check_wait: LockWaitCheck | None = None
    diagnostic: LockDiagnostic | None = None
    clock: Callable[[], float] = time.monotonic


def _acquire(
    descriptor: int,
    protected_path: Path,
    options: FileLockOptions,
) -> tuple[float, bool]:
    started_at = options.clock()
    reported_wait = False
    while True:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return started_at, reported_wait
        except BlockingIOError as error:
            elapsed = max(0.0, options.clock() - started_at)
            if options.check_wait is not None:
                options.check_wait(protected_path, elapsed)
            if elapsed >= options.timeout:
                raise FileLockTimeout(
                    f"timed out waiting for file lock on {protected_path} "
                    f"after {elapsed:.1f}s"
                ) from error
            if options.diagnostic is not None and elapsed >= 1.0 and not reported_wait:
                options.diagnostic(
                    f"Waiting for file lock on {protected_path}; "
                    f"contention has lasted {elapsed:.1f}s"
                )
                reported_wait = True
            time.sleep(
                min(
                    options.poll_interval,
                    max(0.0, options.timeout - elapsed),
                )
            )


@contextmanager
def advisory_file_lock(
    protected_path: Path,
    *,
    lock_path: Path | None = None,
    legacy_lock_path: Path | None = None,
    options: FileLockOptions | None = None,
) -> Generator[None]:
    """Hold a kernel-released exclusive lock on a dedicated sibling inode."""

    options = options or FileLockOptions()
    if options.poll_interval < 0:
        raise ValueError("file-lock poll interval cannot be negative")
    if options.timeout <= 0:
        raise ValueError("file-lock timeout must be positive")

    effective_lock_path = lock_path or Path(f"{protected_path}.bkg-lock")
    effective_lock_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(effective_lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    acquired = False

    try:
        started_at, reported_wait = _acquire(descriptor, protected_path, options)
        acquired = True
        if legacy_lock_path is not None:
            with suppress(FileNotFoundError):
                legacy_lock_path.unlink()
        if options.diagnostic is not None and reported_wait:
            elapsed = max(0.0, options.clock() - started_at)
            options.diagnostic(
                f"Acquired file lock on {protected_path} after {elapsed:.1f}s"
            )
        yield
    finally:
        if acquired:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
