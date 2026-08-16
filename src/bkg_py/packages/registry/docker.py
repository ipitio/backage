"""Optional Docker daemon fallback for cumulative local container sizes."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from ...runtime import CommandOptions, CommandResult
from ..enrichment import (
    DeduplicatedDiagnostics,
    RequestCircuit,
    RequestCircuitLease,
)
from .artifacts import SingleFlightCache

DiagnosticSink = Callable[[str], None]


def _ignore_diagnostic(_message: str) -> None:
    pass


class CommandRunner(Protocol):  # pylint: disable=too-few-public-methods
    """Supervised command operation used by the Docker fallback."""

    def run(
        self,
        command: Sequence[str],
        *,
        options: CommandOptions | None = None,
    ) -> CommandResult:
        """Run one command and return its bounded captured result."""

        raise NotImplementedError


@dataclass(frozen=True)
class DockerSizeSettings:
    """Opt-in and resource bounds for local Docker image inspection."""

    enabled: bool = False
    platform: str = "linux/amd64"
    pull_timeout: float = 300.0
    command_timeout: float = 30.0

    def __post_init__(self) -> None:
        if not self.platform:
            raise ValueError("Docker size platform cannot be empty")
        if self.pull_timeout <= 0:
            raise ValueError("Docker pull timeout must be positive")
        if self.command_timeout <= 0:
            raise ValueError("Docker command timeout must be positive")


@dataclass(frozen=True)
class _DockerImage:
    image_id: str
    size: int


class _DockerCircuitUnavailable(RuntimeError):
    """The Docker fallback circuit is suppressing commands."""


class _DockerTransientError(RuntimeError):
    """The Docker CLI or daemon may recover after a cooldown."""


class _DockerResponseError(RuntimeError):
    """Docker returned output that cannot provide a trustworthy size."""


class DockerSizeInspector:  # pylint: disable=too-few-public-methods
    """Pull absent images, inspect cumulative size, and remove bkg-only pulls."""

    def __init__(
        self,
        runner: CommandRunner,
        circuit: RequestCircuit,
        settings: DockerSizeSettings,
        *,
        diagnostic: DiagnosticSink = _ignore_diagnostic,
    ) -> None:
        self.runner = runner
        self.circuit = circuit
        self.settings = settings
        self.diagnostics = DeduplicatedDiagnostics(diagnostic)
        self._availability = SingleFlightCache[str, bool]()
        self._sizes = SingleFlightCache[str, int]()

    def __call__(self, reference: str) -> int:
        """Return cumulative local bytes or `-1` when fallback is unavailable."""

        if not self.settings.enabled:
            return -1
        try:
            return self._sizes.get(reference, lambda: self._resolve(reference))
        except _DockerCircuitUnavailable:
            return -1

    def _resolve(self, reference: str) -> int:
        with self.circuit.request("docker") as lease:
            if not lease:
                raise _DockerCircuitUnavailable
            try:
                if not self._availability.get("daemon", self._daemon_available):
                    lease.record_success()
                    return -1
                image = self._inspect(reference)
                if image is not None:
                    lease.record_success()
                    return image.size
                size = self._pull_inspect_cleanup(reference)
            except (_DockerTransientError, OSError) as error:
                self._record_transient_failure(lease, error, reference)
                return -1
            except _DockerResponseError as error:
                lease.record_success()
                self.diagnostics.report(
                    f"response:{type(error).__name__}",
                    f"Docker artifact sizing could not inspect {reference}: {error}",
                )
                return -1
            lease.record_success()
            return size

    def _daemon_available(self) -> bool:
        try:
            result = self.runner.run(
                ["docker", "info", "--format", "{{json .ServerVersion}}"],
                options=CommandOptions(timeout=self.settings.command_timeout),
            )
        except OSError as error:
            self.diagnostics.report(
                "capability:cli",
                f"Docker artifact sizing disabled for this run: {error}",
            )
            return False
        if result.timed_out:
            self.diagnostics.report(
                "capability:timeout",
                "Docker artifact sizing disabled for this run: daemon probe timed out",
            )
            return False
        if result.returncode != 0:
            self.diagnostics.report(
                "capability:daemon",
                "Docker artifact sizing disabled for this run: daemon unavailable",
            )
            return False
        return True

    def _inspect(self, reference: str) -> _DockerImage | None:
        result = self.runner.run(
            [
                "docker",
                "image",
                "inspect",
                "--format",
                "{{.Id}} {{.Size}}",
                reference,
            ],
            options=CommandOptions(timeout=self.settings.command_timeout),
        )
        if result.timed_out:
            raise _DockerTransientError("image inspection timed out")
        if result.returncode != 0:
            return None
        return _parse_image(result.stdout)

    def _pull_inspect_cleanup(self, reference: str) -> int:
        try:
            result = self.runner.run(
                [
                    "docker",
                    "image",
                    "pull",
                    "--quiet",
                    "--platform",
                    self.settings.platform,
                    reference,
                ],
                options=CommandOptions(timeout=self.settings.pull_timeout),
            )
            if result.timed_out:
                raise _DockerTransientError("image pull timed out")
            if result.returncode != 0:
                if not self._daemon_responds():
                    raise _DockerTransientError("daemon became unavailable")
                self.diagnostics.report(
                    "pull:image",
                    f"Docker could not pull {reference}; leaving size unknown",
                )
                return -1
            image = self._inspect(reference)
            if image is None:
                raise _DockerResponseError("pulled image was not inspectable")
            return image.size
        finally:
            self._cleanup(reference)

    def _daemon_responds(self) -> bool:
        try:
            result = self.runner.run(
                ["docker", "info", "--format", "{{json .ServerVersion}}"],
                options=CommandOptions(timeout=self.settings.command_timeout),
            )
        except OSError:
            return False
        return not result.timed_out and result.returncode == 0

    def _cleanup(self, reference: str) -> None:
        try:
            result = self.runner.run(
                ["docker", "image", "rm", "--force", reference],
                options=CommandOptions(
                    timeout=self.settings.command_timeout,
                    interruptible=False,
                ),
            )
        except (OSError, RuntimeError) as error:
            self.diagnostics.report(
                "cleanup:error",
                f"Docker could not clean up the image pulled for sizing: {error}",
            )
            return
        if result.timed_out or (
            result.returncode != 0 and self._cleanup_reference_exists(reference)
        ):
            self.diagnostics.report(
                "cleanup:failed",
                f"Docker could not clean up {reference} after artifact sizing",
            )

    def _cleanup_reference_exists(self, reference: str) -> bool:
        try:
            result = self.runner.run(
                [
                    "docker",
                    "image",
                    "inspect",
                    "--format",
                    "{{.Id}}",
                    reference,
                ],
                options=CommandOptions(
                    timeout=self.settings.command_timeout,
                    interruptible=False,
                ),
            )
        except OSError, RuntimeError:
            return True
        return result.timed_out or result.returncode == 0

    def _record_transient_failure(
        self,
        lease: RequestCircuitLease,
        error: Exception,
        reference: str,
    ) -> None:
        cooldown = lease.record_transient_failure()
        self.diagnostics.report(
            f"transient:{type(error).__name__}",
            f"Docker artifact sizing temporarily failed for {reference}: {error}",
        )
        if cooldown is not None:
            self.diagnostics.report(
                f"cooldown:{cooldown}",
                "Docker artifact-size requests paused for "
                f"{cooldown:.0f}s after repeated failures",
            )


def _parse_image(output: bytes) -> _DockerImage:
    try:
        line = output.decode("utf-8").strip()
    except UnicodeDecodeError as error:
        raise _DockerResponseError("image inspection was not UTF-8") from error
    image_id, separator, raw_size = line.partition(" ")
    if not separator or not image_id or not raw_size.isdecimal():
        raise _DockerResponseError("image inspection returned malformed output")
    return _DockerImage(image_id, int(raw_size))
