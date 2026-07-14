"""CLI adapter for the top-level Python run coordinator."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from ..application import ApplicationContext
from ..database import DatabaseError
from ..discovery import DiscoveryError
from ..github import GitHubError
from ..result import ExitStatus
from ..runtime import CommandOptions, GracefulStop, StopController
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


def run_application(
    args: argparse.Namespace,
    application: ApplicationContext,
) -> ExitStatus:
    """Run the complete Python-owned application lifecycle."""

    started = datetime.now(UTC)
    started_at = int(started.timestamp())
    config = replace(
        application.config,
        mode=(application.config.mode if args.mode is None else args.mode),
        max_len=(
            application.config.max_len if args.duration is None else args.duration
        ),
    )
    application.config = config
    application.stop = StopController(
        application.state,
        max_duration=config.max_len,
        started_at_epoch=started_at,
    )

    output = LockedRunOutput(_stdout, _stderr)
    execution = RunApplicationExecution(
        output.progress,
        output.diagnostic,
        _owner_materializer(application, output),
    )
    request = RunCoordinatorRequest(
        today=started.date().isoformat(),
        started_at=started_at,
        mode=config.mode,
        github_owner=config.github_owner,
        source_published_today=args.source_published_today == "true",
        working_directory=Path(args.working_directory),
        owner_request_limit=args.owner_request_limit,
    )

    try:
        with application.stop.signal_handlers():
            if RunMode(config.mode) is RunMode.CLEAN:
                phases = RunApplicationOperations(application, execution)
                status = RunCoordinator(
                    application.state,
                    phases,
                    _coordinator_execution(output),
                ).run(request)
            else:
                with application.github_client() as client:
                    phases = RunApplicationOperations(
                        application,
                        execution,
                        github_client=client,
                    )
                    status = RunCoordinator(
                        application.state,
                        phases,
                        _coordinator_execution(output),
                    ).run(request)
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
