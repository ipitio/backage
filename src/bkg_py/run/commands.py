"""CLI adapter for the top-level Python run coordinator."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from ..application import ApplicationContext
from ..config import RuntimeConfig
from ..database import DatabaseError
from ..discovery import DiscoveryError
from ..github import GitHubError
from ..result import ExitStatus
from ..runtime import CommandOptions, GracefulStop
from ..snapshots import SnapshotError
from ..state import StateValueError
from .application import (
    LockedRunOutput,
    RunApplicationExecution,
    RunApplicationOperations,
)
from .coordinator import (
    RunCoordinator,
    RunCoordinatorExecution,
    RunCoordinatorRequest,
    RunMode,
)


@dataclass(frozen=True)
class RunCommandOptions:
    """Typed inputs for one complete application run."""

    duration: int | None = None
    mode: int | None = None
    source_published_today: bool = False
    working_directory: Path = Path()
    owner_request_limit: int = 100


@dataclass(frozen=True)
class PreparedApplicationRun:
    """Final runtime configuration and coordinator request for one run."""

    config: RuntimeConfig
    request: RunCoordinatorRequest


def run_application(
    args: argparse.Namespace,
    application: ApplicationContext,
) -> ExitStatus:
    """Adapt parsed CLI arguments to the typed application run interface."""

    return execute_application(
        RunCommandOptions(
            duration=args.duration,
            mode=args.mode,
            source_published_today=args.source_published_today == "true",
            working_directory=Path(args.working_directory),
            owner_request_limit=args.owner_request_limit,
        ),
        application,
    )


def execute_application(
    options: RunCommandOptions,
    application: ApplicationContext,
) -> ExitStatus:
    """Run the complete Python-owned application lifecycle."""

    prepared = prepare_application_run(options, application)
    return execute_prepared_application(prepared, application)


def prepare_application_run(
    options: RunCommandOptions,
    application: ApplicationContext,
    *,
    started: datetime | None = None,
) -> PreparedApplicationRun:
    """Configure shared services and build an immutable coordinator request."""

    started = started or datetime.now(UTC)
    started_at = int(started.timestamp())
    config = replace(
        application.config,
        mode=(application.config.mode if options.mode is None else options.mode),
        max_len=(
            application.config.max_len if options.duration is None else options.duration
        ),
    )
    application.configure_run(
        config,
        started_at_epoch=started_at,
    )
    request = RunCoordinatorRequest(
        today=started.date().isoformat(),
        started_at=started_at,
        mode=config.mode,
        github_owner=config.github_owner,
        source_published_today=options.source_published_today,
        working_directory=options.working_directory,
        owner_request_limit=options.owner_request_limit,
    )
    return PreparedApplicationRun(config, request)


def execute_prepared_application(
    prepared: PreparedApplicationRun,
    application: ApplicationContext,
) -> ExitStatus:
    """Execute a run after its final stop controller has been installed."""

    output = LockedRunOutput(_stdout, _stderr)
    execution = RunApplicationExecution(
        output.progress,
        output.diagnostic,
        _owner_materializer(application, output),
    )

    try:
        with application.stop.signal_handlers():
            if RunMode(prepared.config.mode) is RunMode.CLEAN:
                phases = RunApplicationOperations(application, execution)
                status = RunCoordinator(
                    application.state,
                    phases,
                    _coordinator_execution(output),
                ).run(prepared.request)
            else:
                with application.github_client(report=output.progress) as client:
                    phases = RunApplicationOperations(
                        application,
                        execution,
                        github_client=client,
                    )
                    status = RunCoordinator(
                        application.state,
                        phases,
                        _coordinator_execution(output),
                    ).run(prepared.request)
        return ExitStatus(status)
    except GracefulStop as error:
        reason = str(error) or "requested"
        output.diagnostic(f"Graceful stop requested: {reason}")
        return ExitStatus.GRACEFUL_STOP
    except (
        DatabaseError,
        DiscoveryError,
        GitHubError,
        OSError,
        SnapshotError,
        StateValueError,
        ValueError,
    ) as error:
        output.diagnostic(str(error))
        return ExitStatus.NON_FATAL


def _coordinator_execution(output: LockedRunOutput) -> RunCoordinatorExecution:
    return RunCoordinatorExecution(output.progress, output.diagnostic)


def _owner_materializer(
    application: ApplicationContext,
    output: LockedRunOutput,
) -> Callable[[tuple[str, ...]], None]:
    def materialize(owners: tuple[str, ...]) -> None:
        if not owners:
            return
        script = Path(application.config.root) / "src/lib/materialize-owner-trees.sh"
        result = application.process_runner.run(
            ("bash", str(script), *owners),
            options=CommandOptions(cwd=application.config.root),
        )
        _forward_output(result.stdout, output.progress)
        _forward_output(result.stderr, output.diagnostic)
        if result.returncode != 0:
            raise OSError(
                f"Owner tree materialization failed with status {result.returncode}"
            )

    return materialize


def _forward_output(content: bytes, sink: Callable[[str], None]) -> None:
    for line in content.decode("utf-8", errors="replace").splitlines():
        sink(line)


def _stdout(message: str) -> None:
    sys.stdout.write(f"{message}\n")
    sys.stdout.flush()


def _stderr(message: str) -> None:
    sys.stderr.write(f"{message}\n")
    sys.stderr.flush()
