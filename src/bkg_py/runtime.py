"""Runtime stop control and supervised subprocess execution."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from types import FrameType

from .files import atomic_path
from .result import ExitStatus
from .state import StateStore

_TIMEOUT_KEY = "BKG_TIMEOUT"
_SCRIPT_START_KEY = "BKG_SCRIPT_START"
SignalHandler = Callable[[int, FrameType | None], object] | int | signal.Handlers | None


class GracefulStop(RuntimeError):
    """The current operation should stop after leaving durable state resumable."""

    status = ExitStatus.GRACEFUL_STOP


@dataclass(frozen=True)
class CommandResult:
    """Captured outcome of a supervised command."""

    args: tuple[str, ...]
    returncode: int
    stdout: bytes
    stderr: bytes


@dataclass(frozen=True)
class RuntimeTiming:
    """Injectable clocks and polling cadence for runtime control."""

    clock: Callable[[], float] = time.monotonic
    wall_clock: Callable[[], float] = time.time
    poll_interval: float = 0.1


@dataclass(frozen=True)
class CommandOptions:
    """Optional execution settings for a supervised command."""

    cwd: str | os.PathLike[str] | None = None
    env: Mapping[str, str] | None = None
    combine_output: bool = False


def resolve_executable(
    executable: str | os.PathLike[str],
    *,
    cwd: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
) -> str:
    """Return the absolute executable path used for a child process."""

    value = os.fspath(executable)
    if os.sep in value or (os.altsep is not None and os.altsep in value):
        path = Path(value)
        if not path.is_absolute():
            base = Path.cwd() if cwd is None else Path(cwd)
            path = base / path
        return str(path.resolve())

    search_path = os.pathsep.join(os.get_exec_path(env))
    resolved = shutil.which(value, path=search_path)
    if resolved is None:
        raise FileNotFoundError(f"executable not found on PATH: {value}")
    return str(Path(resolved).resolve())


@dataclass
class PhaseTimer:
    """Measure one runtime phase with a monotonic clock."""

    name: str
    clock: Callable[[], float] = time.monotonic
    _started_at: float = field(init=False)

    def __post_init__(self) -> None:
        self._started_at = self.clock()

    @property
    def elapsed(self) -> float:
        """Return elapsed monotonic seconds."""

        return max(0.0, self.clock() - self._started_at)

    def message(self) -> str:
        """Return a concise Action-log-friendly completion message."""

        return f"{self.name} completed in {self.elapsed:.1f}s"


class StopController:
    """Combine persisted, elapsed-time, and signal-driven stop requests."""

    def __init__(
        self,
        state: StateStore,
        *,
        max_duration: float,
        started_at_epoch: float | None = None,
        timing: RuntimeTiming | None = None,
    ) -> None:
        self.state = state
        self.max_duration = max_duration
        self.timing = timing or RuntimeTiming()
        self._event = threading.Event()
        self._reason: str | None = None
        self._started_at = self.timing.clock()

        persisted_start = self.state.get(_SCRIPT_START_KEY)
        if started_at_epoch is None and persisted_start:
            try:
                started_at_epoch = float(persisted_start)
            except ValueError:
                started_at_epoch = None
        self._initial_elapsed = (
            max(0.0, self.timing.wall_clock() - started_at_epoch)
            if started_at_epoch is not None
            else 0.0
        )

    @property
    def elapsed(self) -> float:
        """Return elapsed seconds including time before controller creation."""

        return self._initial_elapsed + max(
            0.0,
            self.timing.clock() - self._started_at,
        )

    @property
    def reason(self) -> str | None:
        """Return the first local reason for requesting a stop."""

        return self._reason

    def request_stop(self, reason: str = "requested") -> None:
        """Persist and remember a graceful stop request."""

        if self._reason is None:
            self._reason = reason
        self._event.set()
        if self.state.get(_TIMEOUT_KEY) != "1":
            self.state.set(_TIMEOUT_KEY, "1")

    def is_requested(self) -> bool:
        """Return whether persisted state, elapsed time, or a signal requests stop."""

        if self._event.is_set():
            self.request_stop(self._reason or "requested")
            return True
        if self.state.get(_TIMEOUT_KEY) == "1":
            self._event.set()
            if self._reason is None:
                self._reason = "persisted"
            return True
        if self.max_duration > 0 and self.elapsed >= self.max_duration:
            self.request_stop("elapsed")
            return True
        return False

    def check(self) -> None:
        """Raise the shared graceful-stop exception when work should stop."""

        if self.is_requested():
            raise GracefulStop(self._reason or "requested")

    def sleep(self, seconds: float) -> None:
        """Sleep interruptibly while observing external and elapsed stop state."""

        deadline = self.timing.clock() + max(0.0, seconds)
        while True:
            self.check()
            remaining = deadline - self.timing.clock()
            if remaining <= 0:
                return
            self._event.wait(min(self.timing.poll_interval, remaining))

    @contextmanager
    def finalization_scope(self) -> Generator[None, None, None]:
        """Defer an existing stop while allowing a new stop to interrupt cleanup."""

        previous_timeout = self.state.get(_TIMEOUT_KEY)
        previous_requested = self._event.is_set() or previous_timeout == "1"
        previous_reason = self._reason
        previous_max_duration = self.max_duration
        completed_normally = False

        self._event.clear()
        self._reason = None
        self.max_duration = -1
        self.state.set(_TIMEOUT_KEY, 0)
        try:
            yield
            completed_normally = True
        finally:
            newly_requested = (
                self._event.is_set() or self.state.get(_TIMEOUT_KEY) == "1"
            )
            new_reason = self._reason
            self.max_duration = previous_max_duration
            self._event.clear()

            if newly_requested:
                self._event.set()
                self._reason = new_reason or "requested"
                self.state.set(_TIMEOUT_KEY, 1)
                if completed_normally:
                    raise GracefulStop(self._reason)
            else:
                self._reason = previous_reason
                if previous_requested:
                    self._event.set()
                if previous_timeout is None:
                    self.state.delete(_TIMEOUT_KEY)
                else:
                    self.state.set(_TIMEOUT_KEY, previous_timeout)

    @contextmanager
    def signal_handlers(
        self,
        handled_signals: Sequence[signal.Signals] = (
            signal.SIGINT,
            signal.SIGTERM,
        ),
    ) -> Generator[None, None, None]:
        """Translate process termination signals into graceful stop requests."""

        if threading.current_thread() is not threading.main_thread():
            yield
            return

        previous: dict[signal.Signals, SignalHandler] = {}

        def handle(signum: int, _frame: FrameType | None) -> None:
            if self._reason is None:
                self._reason = f"signal-{signum}"
            self._event.set()

        try:
            for handled_signal in handled_signals:
                previous[handled_signal] = signal.getsignal(handled_signal)
                signal.signal(handled_signal, handle)
            yield
        finally:
            for handled_signal, handler in previous.items():
                signal.signal(handled_signal, handler)


class ProcessRunner:
    """Run commands in isolated process groups and stop them predictably."""

    def __init__(
        self,
        stop: StopController,
        *,
        poll_interval: float = 0.1,
        termination_grace: float = 5.0,
    ) -> None:
        self.stop = stop
        self.poll_interval = poll_interval
        self.termination_grace = termination_grace

    def run(
        self,
        command: Sequence[str | os.PathLike[str]],
        *,
        options: CommandOptions | None = None,
    ) -> CommandResult:
        """Run a command and capture output while observing graceful stop."""

        return self._run(
            command,
            options=options or CommandOptions(),
            stdout_path=None,
        )

    def run_to_file(
        self,
        command: Sequence[str | os.PathLike[str]],
        destination: Path,
        *,
        options: CommandOptions | None = None,
    ) -> CommandResult:
        """Run a command and publish its stdout only after successful completion."""

        command_options = options or CommandOptions()

        class DiscardOutput(Exception):
            """Prevent an unsuccessful command from replacing good output."""

            def __init__(self, result: CommandResult) -> None:
                super().__init__()
                self.result = result

        try:
            with atomic_path(destination) as temporary_path:
                result = self._run(
                    command,
                    options=command_options,
                    stdout_path=temporary_path,
                )
                if result.returncode != 0:
                    raise DiscardOutput(result)
                return result
        except DiscardOutput as error:
            return error.result

    def _run(
        self,
        command: Sequence[str | os.PathLike[str]],
        *,
        options: CommandOptions,
        stdout_path: Path | None,
    ) -> CommandResult:
        if not command:
            raise ValueError("command must not be empty")
        args = (
            resolve_executable(command[0], cwd=options.cwd, env=options.env),
            *(os.fspath(part) for part in command[1:]),
        )
        self.stop.check()

        with (
            (
                stdout_path.open("w+b")
                if stdout_path is not None
                else tempfile.TemporaryFile(mode="w+b")
            ) as stdout_file,
            tempfile.TemporaryFile(mode="w+b") as stderr_file,
            # The executable is resolved and argv is passed without a shell.
            subprocess.Popen(  # noqa: S603
                args,
                cwd=options.cwd,
                env=dict(options.env) if options.env is not None else None,
                stdout=stdout_file,
                stderr=(subprocess.STDOUT if options.combine_output else stderr_file),
                shell=False,
                start_new_session=True,
            ) as process,
        ):
            try:
                while process.poll() is None:
                    self.stop.sleep(self.poll_interval)
            except BaseException:
                self._terminate(process)
                raise

            stdout = b""
            if stdout_path is None:
                stdout_file.seek(0)
                stdout = stdout_file.read()
            stderr = b""
            if not options.combine_output:
                stderr_file.seek(0)
                stderr = stderr_file.read()
            returncode = process.returncode

        return CommandResult(
            args=args,
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
        )

    def _terminate(self, process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return

        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)

        try:
            process.wait(timeout=self.termination_grace)
            return
        except subprocess.TimeoutExpired:
            pass

        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait()
