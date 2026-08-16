"""Tests for optional cumulative local sizing through a Docker daemon."""

from __future__ import annotations

from collections.abc import Iterable, Sequence

import pytest

from bkg_py.packages.enrichment import RequestCircuit, RequestCircuitSettings
from bkg_py.packages.registry.docker import DockerSizeInspector, DockerSizeSettings
from bkg_py.runtime import CommandOptions, CommandResult, GracefulStop


class _FakeCommandRunner:  # pylint: disable=too-few-public-methods
    def __init__(self, responses: Iterable[CommandResult | Exception]) -> None:
        self.responses = list(responses)
        self.commands: list[tuple[tuple[str, ...], CommandOptions | None]] = []

    def run(
        self,
        command: Sequence[str],
        *,
        options: CommandOptions | None = None,
    ) -> CommandResult:
        """Return the next command result while recording execution options."""

        self.commands.append((tuple(command), options))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _result(
    returncode: int = 0,
    *,
    stdout: bytes = b"",
    stderr: bytes = b"",
    timed_out: bool = False,
) -> CommandResult:
    return CommandResult((), returncode, stdout, stderr, timed_out)


def _inspector(
    runner: _FakeCommandRunner,
    *,
    diagnostics: list[str] | None = None,
    circuit: RequestCircuit | None = None,
) -> DockerSizeInspector:
    return DockerSizeInspector(
        runner,
        circuit or RequestCircuit(),
        DockerSizeSettings(
            enabled=True,
            platform="linux/amd64",
            pull_timeout=40,
            command_timeout=5,
        ),
        diagnostic=(
            diagnostics.append if diagnostics is not None else lambda _msg: None
        ),
    )


def test_docker_fallback_is_inert_until_enabled() -> None:
    """The default configuration never probes a local CLI or daemon."""

    runner = _FakeCommandRunner([])
    inspector = DockerSizeInspector(
        runner,
        RequestCircuit(),
        DockerSizeSettings(),
    )

    assert inspector("ghcr.io/example/demo:latest") == -1
    assert not runner.commands


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (FileNotFoundError("docker missing"), "docker missing"),
        (_result(1), "daemon unavailable"),
        (_result(143, timed_out=True), "daemon probe timed out"),
    ],
)
def test_missing_cli_or_daemon_is_cached_for_the_process(
    response: CommandResult | Exception,
    message: str,
) -> None:
    """An unavailable Docker capability is diagnosed once without repeated probes."""

    runner = _FakeCommandRunner([response])
    diagnostics: list[str] = []
    inspector = _inspector(runner, diagnostics=diagnostics)

    assert inspector("ghcr.io/example/one:latest") == -1
    assert inspector("ghcr.io/example/two:latest") == -1

    assert len(runner.commands) == 1
    assert message in diagnostics[0]


def test_preexisting_image_is_inspected_without_pull_or_cleanup() -> None:
    """A local image is read without changing the daemon's existing state."""

    runner = _FakeCommandRunner(
        [
            _result(stdout=b'"29.1"\n'),
            _result(stdout=b"sha256:existing 8192\n"),
        ]
    )
    inspector = _inspector(runner)
    reference = "ghcr.io/example/demo:latest"

    assert inspector(reference) == 8_192
    assert inspector(reference) == 8_192

    assert len(runner.commands) == 2
    assert runner.commands[1][0][1:3] == ("image", "inspect")


def test_absent_image_is_pulled_inspected_and_removed() -> None:
    """An image introduced solely for sizing is always removed afterward."""

    runner = _FakeCommandRunner(
        [
            _result(stdout=b'"29.1"\n'),
            _result(1),
            _result(stdout=b"sha256:pulled\n"),
            _result(stdout=b"sha256:pulled 123456\n"),
            _result(),
        ]
    )
    inspector = _inspector(runner)
    reference = "ghcr.io/example/demo:latest"

    assert inspector(reference) == 123_456

    pull_command, pull_options = runner.commands[2]
    assert pull_command == (
        "docker",
        "image",
        "pull",
        "--quiet",
        "--platform",
        "linux/amd64",
        reference,
    )
    assert pull_options is not None
    assert pull_options.timeout == 40
    cleanup_command, cleanup_options = runner.commands[-1]
    assert cleanup_command == ("docker", "image", "rm", "--force", reference)
    assert cleanup_options is not None
    assert not cleanup_options.interruptible


def test_image_specific_pull_failure_remains_nonfatal_and_cleans_up() -> None:
    """A rejected image does not open the daemon-wide recovery circuit."""

    runner = _FakeCommandRunner(
        [
            _result(stdout=b'"29.1"\n'),
            _result(1),
            _result(1, stderr=b"manifest unknown"),
            _result(stdout=b'"29.1"\n'),
            _result(),
        ]
    )
    diagnostics: list[str] = []
    inspector = _inspector(runner, diagnostics=diagnostics)

    assert inspector("ghcr.io/example/missing:latest") == -1
    assert "could not pull" in diagnostics[0]
    assert runner.commands[-1][0][1:3] == ("image", "rm")


def test_absent_reference_after_failed_cleanup_is_not_reported_as_a_leak() -> None:
    """A failed removal is benign when a follow-up inspect confirms no image."""

    runner = _FakeCommandRunner(
        [
            _result(stdout=b'"29.1"\n'),
            _result(1),
            _result(1, stderr=b"manifest unknown"),
            _result(stdout=b'"29.1"\n'),
            _result(1),
            _result(1),
        ]
    )
    diagnostics: list[str] = []
    inspector = _inspector(runner, diagnostics=diagnostics)

    assert inspector("ghcr.io/example/missing:latest") == -1
    assert not any("clean up" in message for message in diagnostics)
    assert runner.commands[-1][0][1:3] == ("image", "inspect")


def test_pull_timeouts_clean_up_and_open_only_the_docker_circuit() -> None:
    """Repeated pull timeouts pause Docker sizing after bounded cleanup."""

    runner = _FakeCommandRunner(
        [
            _result(stdout=b'"29.1"\n'),
            _result(1),
            _result(143, timed_out=True),
            _result(),
            _result(1),
            _result(143, timed_out=True),
            _result(),
        ]
    )
    diagnostics: list[str] = []
    inspector = _inspector(
        runner,
        diagnostics=diagnostics,
        circuit=RequestCircuit(
            RequestCircuitSettings(
                max_concurrent=1,
                failure_threshold=2,
                cooldown_seconds=300,
            )
        ),
    )

    assert inspector("ghcr.io/example/one:latest") == -1
    assert inspector("ghcr.io/example/two:latest") == -1
    assert inspector("ghcr.io/example/three:latest") == -1

    assert len(runner.commands) == 7
    assert sum(command[1:3] == ("image", "rm") for command, _ in runner.commands) == 2
    assert any("paused for 300s" in message for message in diagnostics)


def test_graceful_stop_during_pull_still_runs_bounded_cleanup() -> None:
    """A stopped pull propagates status control after removing its image reference."""

    runner = _FakeCommandRunner(
        [
            _result(stdout=b'"29.1"\n'),
            _result(1),
            GracefulStop("elapsed"),
            _result(),
        ]
    )
    inspector = _inspector(runner)
    reference = "ghcr.io/example/demo:latest"

    with pytest.raises(GracefulStop, match="elapsed"):
        inspector(reference)

    cleanup_command, cleanup_options = runner.commands[-1]
    assert cleanup_command == ("docker", "image", "rm", "--force", reference)
    assert cleanup_options is not None
    assert not cleanup_options.interruptible
