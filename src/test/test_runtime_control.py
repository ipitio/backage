"""Tests for runtime stop control and supervised commands."""

from __future__ import annotations

import os
from pathlib import Path
import signal
import sys
import tempfile
import threading
import time
import unittest

from bkg_py.result import ExitStatus
from bkg_py.runtime import (
    CommandOptions,
    GracefulStop,
    PhaseTimer,
    ProcessRunner,
    RuntimeTiming,
    StopController,
)
from bkg_py.state import StateStore


class FakeClock:
    """A manually advanced monotonic clock."""

    def __init__(self, value: float = 0.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        """Advance the clock."""

        self.value += seconds


def _is_running(pid: int) -> bool:
    try:
        status = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()[2]
    except (FileNotFoundError, IndexError, ProcessLookupError):
        return False
    return status != "Z"


class RuntimeControlTests(unittest.TestCase):
    """Exercise elapsed, persisted, signal, and child-process stop paths."""

    def test_elapsed_limit_persists_graceful_stop(self) -> None:
        """A monotonic elapsed limit writes timeout state and raises status 3."""

        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "env.env"
            state_path.touch()
            clock = FakeClock(10.0)
            controller = StopController(
                StateStore(state_path),
                max_duration=5,
                timing=RuntimeTiming(clock=clock),
            )

            clock.advance(5)

            with self.assertRaises(GracefulStop) as raised:
                controller.check()
            self.assertEqual(raised.exception.status, ExitStatus.GRACEFUL_STOP)
            self.assertEqual(StateStore(state_path).get("BKG_TIMEOUT"), "1")
            self.assertEqual(controller.reason, "elapsed")

    def test_persisted_stop_is_observed_immediately(self) -> None:
        """A stop requested by another process is visible to Python work."""

        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "env.env"
            state_path.write_text("BKG_TIMEOUT=1\n\n", encoding="utf-8")
            controller = StopController(StateStore(state_path), max_duration=0)

            with self.assertRaises(GracefulStop):
                controller.check()
            self.assertEqual(controller.reason, "persisted")

    def test_signal_requests_and_persists_stop(self) -> None:
        """SIGTERM becomes a graceful stop instead of terminating the process."""

        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "env.env"
            state_path.touch()
            controller = StopController(StateStore(state_path), max_duration=0)

            with controller.signal_handlers((signal.SIGTERM,)):
                os.kill(os.getpid(), signal.SIGTERM)
                with self.assertRaises(GracefulStop):
                    controller.check()

            self.assertEqual(StateStore(state_path).get("BKG_TIMEOUT"), "1")
            self.assertEqual(controller.reason, f"signal-{signal.SIGTERM}")

    def test_sleep_observes_external_stop(self) -> None:
        """Interruptible sleep notices a persisted request without waiting fully."""

        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "env.env"
            state_path.touch()
            state = StateStore(state_path, lock_poll_interval=0.001)
            controller = StopController(
                state,
                max_duration=0,
                timing=RuntimeTiming(poll_interval=0.01),
            )

            requester = threading.Thread(
                target=lambda: (time.sleep(0.05), state.set("BKG_TIMEOUT", "1")),
            )
            requester.start()
            started_at = time.monotonic()
            with self.assertRaises(GracefulStop):
                controller.sleep(10)
            requester.join()

            self.assertLess(time.monotonic() - started_at, 1)

    def test_process_runner_captures_output(self) -> None:
        """Successful commands return separate or combined byte streams."""

        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "env.env"
            state_path.touch()
            runner = ProcessRunner(
                StopController(StateStore(state_path), max_duration=0)
            )
            command = [
                sys.executable,
                "-c",
                "import sys; print('out'); print('err', file=sys.stderr)",
            ]

            separate = runner.run(command)
            combined = runner.run(
                command,
                options=CommandOptions(combine_output=True),
            )

            self.assertEqual(separate.returncode, 0)
            self.assertEqual(separate.stdout, b"out\n")
            self.assertEqual(separate.stderr, b"err\n")
            self.assertIn(b"out\n", combined.stdout)
            self.assertIn(b"err\n", combined.stdout)
            self.assertEqual(combined.stderr, b"")

    def test_process_runner_publishes_only_successful_output(self) -> None:
        """Failed commands cannot replace the last complete destination."""

        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "env.env"
            state_path.touch()
            destination = Path(directory) / "output.txt"
            destination.write_text("old\n", encoding="utf-8")
            runner = ProcessRunner(
                StopController(StateStore(state_path), max_duration=0)
            )

            failed = runner.run_to_file(
                [sys.executable, "-c", "print('partial'); raise SystemExit(7)"],
                destination,
            )
            self.assertEqual(failed.returncode, 7)
            self.assertEqual(destination.read_text(encoding="utf-8"), "old\n")

            succeeded = runner.run_to_file(
                [sys.executable, "-c", "print('complete')"],
                destination,
            )
            self.assertEqual(succeeded.returncode, 0)
            self.assertEqual(
                destination.read_text(encoding="utf-8"),
                "complete\n",
            )

    def test_process_runner_kills_blocked_process_group(self) -> None:
        """A stop terminates both a blocked command and its child."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_path = root / "env.env"
            child_pid_path = root / "child.pid"
            state_path.touch()
            state = StateStore(state_path, lock_poll_interval=0.001)
            controller = StopController(
                state,
                max_duration=0,
                timing=RuntimeTiming(poll_interval=0.01),
            )
            runner = ProcessRunner(
                controller,
                poll_interval=0.01,
                termination_grace=0.1,
            )
            script = (
                "import pathlib, signal, subprocess, sys, time;"
                "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
                "child=subprocess.Popen([sys.executable, '-c', "
                '"import signal,time;'
                "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
                'time.sleep(30)"]);'
                f"pathlib.Path({str(child_pid_path)!r}).write_text("
                "str(child.pid), encoding='utf-8');"
                "time.sleep(30)"
            )

            def request_stop() -> None:
                deadline = time.monotonic() + 5
                while not child_pid_path.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                state.set("BKG_TIMEOUT", "1")

            requester = threading.Thread(target=request_stop)
            requester.start()
            started_at = time.monotonic()
            with self.assertRaises(GracefulStop):
                runner.run([sys.executable, "-c", script])
            requester.join()

            self.assertLess(time.monotonic() - started_at, 2)
            child_pid = int(child_pid_path.read_text(encoding="utf-8"))
            deadline = time.monotonic() + 1
            while _is_running(child_pid) and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertFalse(_is_running(child_pid))

    def test_phase_timer_uses_monotonic_time(self) -> None:
        """Phase messages report stable elapsed time."""

        clock = FakeClock(4)
        timer = PhaseTimer("aggregate", clock=clock)
        clock.advance(1.25)

        self.assertEqual(timer.elapsed, 1.25)
        self.assertEqual(timer.message(), "aggregate completed in 1.2s")


if __name__ == "__main__":
    unittest.main()
