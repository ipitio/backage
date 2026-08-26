"""Tests for shared advisory file locking."""

import multiprocessing
import os
import signal
import threading
import time
from pathlib import Path

import pytest

from bkg_py.files import atomic_text_output
from bkg_py.locking import FileLockOptions, FileLockTimeout, advisory_file_lock
from bkg_py.runtime import GracefulStop, StopController
from bkg_py.state import StateStore


def _hold_lock_until_killed(path: str, ready_descriptor: int) -> None:
    with advisory_file_lock(Path(path)):
        os.write(ready_descriptor, b"1")
        while True:
            signal.pause()


def _increment_state(path: str, count: int) -> None:
    state = StateStore(Path(path), lock_poll_interval=0.001)
    for _ in range(count):
        state.increment("BKG_COUNT")


def _short_wait() -> FileLockOptions:
    return FileLockOptions(poll_interval=0.001, timeout=0.05)


def _replace_under_lock_and_fail(path: Path, legacy_lock: Path) -> None:
    with advisory_file_lock(path, legacy_lock_path=legacy_lock):
        with atomic_text_output(path) as file:
            file.write("new\n")
        with (
            pytest.raises(FileLockTimeout),
            advisory_file_lock(path, options=_short_wait()),
        ):
            pass
        raise RuntimeError("operation failed")


def test_live_holder_times_out_without_being_displaced(tmp_path: Path) -> None:
    """A contender cannot remove or bypass a genuine live holder."""

    path = tmp_path / "shared.txt"
    entered = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with advisory_file_lock(path):
            entered.set()
            release.wait()

    holder = threading.Thread(target=hold)
    holder.start()
    assert entered.wait(1)

    with (
        pytest.raises(FileLockTimeout, match=str(path)),
        advisory_file_lock(path, options=_short_wait()),
    ):
        pass

    assert holder.is_alive()
    assert Path(f"{path}.bkg-lock").exists()
    release.set()
    holder.join(1)
    assert not holder.is_alive()


def test_lock_survives_target_replacement_and_releases_after_error(
    tmp_path: Path,
) -> None:
    """Replacing protected data cannot move its lock to another inode."""

    path = tmp_path / "shared.txt"
    path.write_text("old\n", encoding="utf-8")
    legacy_lock = Path(f"{path}.lock")
    legacy_lock.touch()

    with pytest.raises(RuntimeError, match="operation failed"):
        _replace_under_lock_and_fail(path, legacy_lock)

    assert path.read_text(encoding="utf-8") == "new\n"
    assert not legacy_lock.exists()
    with advisory_file_lock(path, options=_short_wait()):
        pass


def test_sigkill_releases_process_lock(tmp_path: Path) -> None:
    """Kernel ownership releases a lock when its process is killed."""

    path = tmp_path / "shared.txt"
    read_descriptor, write_descriptor = os.pipe()
    process = multiprocessing.get_context("fork").Process(
        target=_hold_lock_until_killed,
        args=(str(path), write_descriptor),
    )
    try:
        process.start()
        os.close(write_descriptor)
        assert os.read(read_descriptor, 1) == b"1"
        process.kill()
        process.join(1)
        assert process.exitcode == -signal.SIGKILL

        with advisory_file_lock(path, options=_short_wait()):
            pass
    finally:
        os.close(read_descriptor)
        if process.is_alive():
            process.kill()
            process.join(1)


def test_state_updates_are_serialized_across_processes(tmp_path: Path) -> None:
    """Independent processes cannot lose state read-modify-write updates."""

    path = tmp_path / ".env"
    path.touch()
    processes = [
        multiprocessing.get_context("fork").Process(
            target=_increment_state,
            args=(str(path), 10),
        )
        for _ in range(4)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(2)

    assert all(process.exitcode == 0 for process in processes)
    assert StateStore(path).get_int("BKG_COUNT") == 40


def test_signal_interrupts_state_lock_wait(tmp_path: Path) -> None:
    """A signal returns a contending state update through graceful status 3."""

    path = tmp_path / ".env"
    path.touch()
    entered = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with advisory_file_lock(path):
            entered.set()
            release.wait()

    holder = threading.Thread(target=hold)
    holder.start()
    assert entered.wait(1)

    state = StateStore(path, lock_poll_interval=0.001)
    controller = StopController(state, max_duration=0)
    state.configure_locking(check_wait=controller.check_lock_wait)

    def terminate() -> None:
        time.sleep(0.02)
        os.kill(os.getpid(), signal.SIGTERM)

    sender = threading.Thread(target=terminate)
    started_at = time.monotonic()
    with controller.signal_handlers((signal.SIGTERM,)):
        sender.start()
        with pytest.raises(GracefulStop, match="stopped waiting for file lock"):
            state.set("BKG_VALUE", "blocked")
    sender.join(1)

    assert time.monotonic() - started_at < 1
    assert controller.reason == f"signal-{signal.SIGTERM}"
    release.set()
    holder.join(1)
    with pytest.raises(GracefulStop):
        controller.check()
    assert state.get("BKG_TIMEOUT") == "1"
