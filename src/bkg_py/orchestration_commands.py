"""CLI adapters for Python-owned application orchestration decisions."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from .database import DatabaseError
from .discovery import DiscoveryError
from .github import GitHubError
from .orchestration import BatchRuntimeService, RunOutcomePolicy
from .owners import OwnerBatchRequest
from .result import ExitStatus
from .run import OwnerQueuePhaseRequest, RunCoordinatorRequest
from .run.application import (
    LockedRunOutput,
    RunApplicationExecution,
    RunApplicationOperations,
)
from .run_publication import (
    RunPublicationIdentity,
    RunPublicationPaths,
    RunPublicationRequest,
    RunPublicationService,
)
from .runtime import GracefulStop
from .snapshots import SnapshotError
from .state import StateValueError

if TYPE_CHECKING:
    from .application import ApplicationContext


def run_orchestration(
    args: argparse.Namespace,
    application: ApplicationContext,
) -> ExitStatus:
    """Run one orchestration operation against the shared application state."""

    try:
        if args.orchestration_command == "owner-phase-decision":
            return _owner_phase_decision(args)

        if args.orchestration_command == "update-owners":
            with application.stop.signal_handlers():
                return _update_owners(args, application)

        if args.orchestration_command in {"prepare-run", "prepare-package-plan"}:
            status = _run_planning_command(args, application)
        elif args.orchestration_command == "discover-owners":
            with application.stop.signal_handlers():
                status = _discover_owners(args, application)
        elif args.orchestration_command == "prepare-owner-queue":
            with application.stop.signal_handlers():
                status = _prepare_owner_queue(args, application)
        elif args.orchestration_command in {
            "prepare-targeted-owner-queue",
            "prepare-optout-owner-queue",
        }:
            with application.stop.signal_handlers():
                status = _prepare_targeted_owner_queue(
                    args,
                    application,
                    optouts=(
                        args.orchestration_command == "prepare-optout-owner-queue"
                    ),
                )
        elif args.orchestration_command in {"publish-run-summary", "finalize-run"}:
            status = _run_publication_command(args, application)
        else:
            status = _run_batch_runtime(args, application)
        return status
    except GracefulStop as error:
        reason = str(error) or "requested"
        sys.stderr.write(f"Graceful stop requested: {reason}\n")
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
        sys.stderr.write(f"{error}\n")
        return ExitStatus.NON_FATAL


def _owner_phase_decision(args: argparse.Namespace) -> ExitStatus:
    decision = RunOutcomePolicy.owner_updates(
        args.phase_status,
        args.run_status,
    )
    sys.stdout.write(f"{decision.action}\t{decision.run_status}\t{decision.message}\n")
    return ExitStatus.SUCCESS


def _run_planning_command(
    args: argparse.Namespace,
    application: ApplicationContext,
) -> ExitStatus:
    if args.orchestration_command == "prepare-run":
        return _prepare_run(args, application)
    return _prepare_package_plan(args, application)


def _prepare_package_plan(
    args: argparse.Namespace,
    application: ApplicationContext,
) -> ExitStatus:
    with application.stop.signal_handlers():
        summary = _operations(application).prepare_package_plan(
            args.since,
            Path(args.directory),
            reset=args.reset == "true",
        )
    sys.stdout.write(f"{summary.total}\t{summary.completed}\t{summary.pending}\n")
    return ExitStatus.SUCCESS


def _prepare_run(
    args: argparse.Namespace,
    application: ApplicationContext,
) -> ExitStatus:
    config = application.config
    with application.stop.signal_handlers():
        result = _operations(application, progress=_stderr).prepare_run(
            RunCoordinatorRequest(
                today=args.today,
                started_at=args.started_at,
                mode=config.mode,
                github_owner=config.github_owner,
                source_published_today=False,
                working_directory=Path(args.working_directory),
            )
        )
    summary = result.package_plan
    fast_out = str(result.fast_out).lower()
    sys.stdout.write(
        f"{result.batch_first_started}\t{summary.total}\t{summary.completed}\t"
        f"{summary.pending}\t{result.database_size}\t{result.opted_out}\t"
        f"{fast_out}\n"
    )
    return ExitStatus.SUCCESS


def _run_publication_command(
    args: argparse.Namespace,
    application: ApplicationContext,
) -> ExitStatus:
    if args.orchestration_command == "finalize-run":
        return _finalize_run(args, application)
    return _publish_run_summary(args, application)


def _publication_request(
    args: argparse.Namespace,
    application: ApplicationContext,
    *,
    rotated: bool,
) -> RunPublicationRequest:
    config = application.config
    if config.index_dir is None:
        raise ValueError("BKG_INDEX_DIR is required")
    if config.github_branch is None:
        raise ValueError("GITHUB_BRANCH is required")
    return RunPublicationRequest(
        paths=RunPublicationPaths(
            root=Path(config.root),
            index_directory=Path(config.index_dir),
            working_directory=Path(args.working_directory),
        ),
        identity=RunPublicationIdentity(
            github_owner=config.github_owner,
            github_repo=config.github_repo,
            github_branch=config.github_branch,
        ),
        today=args.today,
        rotated=rotated,
    )


def _publish_run_summary(
    args: argparse.Namespace,
    application: ApplicationContext,
) -> ExitStatus:
    with application.stop.signal_handlers():
        RunPublicationService(
            application.database,
            application.state,
            application.stop.check,
        ).publish(
            _publication_request(
                args,
                application,
                rotated=args.rotated == "true",
            )
        )
    return ExitStatus.SUCCESS


def _finalize_run(
    args: argparse.Namespace,
    application: ApplicationContext,
) -> ExitStatus:
    with application.stop.signal_handlers():
        _operations(application).finalize_run(
            args.today,
            args.prepare_snapshot == "true",
            Path(args.working_directory),
        )
    return ExitStatus.SUCCESS


def _discover_owners(
    args: argparse.Namespace,
    application: ApplicationContext,
) -> ExitStatus:
    _operations(application).discover_owners(
        args.today,
        args.skip_explore == "true",
        Path(args.connections_file),
        Path(args.packages_all_file),
    )
    return ExitStatus.SUCCESS


def _prepare_targeted_owner_queue(
    args: argparse.Namespace,
    application: ApplicationContext,
    *,
    optouts: bool,
) -> ExitStatus:
    operations = _operations(application)
    if optouts:
        operations.prepare_optout_owner_queue()
    else:
        operations.prepare_targeted_owner_queue(Path(args.connections_file))
    return ExitStatus.SUCCESS


def _prepare_owner_queue(
    args: argparse.Namespace,
    application: ApplicationContext,
) -> ExitStatus:
    _operations(application).prepare_owner_queue(
        OwnerQueuePhaseRequest(
            rest_first=args.rest_first,
            connections_file=Path(args.connections_file),
            request_limit=args.request_limit,
            include_manual=args.include_manual == "true",
            working_directory=Path(args.working_directory),
            now=args.now,
        )
    )
    return ExitStatus.SUCCESS


def _run_batch_runtime(
    args: argparse.Namespace,
    application: ApplicationContext,
) -> ExitStatus:
    application.ensure_state_file()
    service = BatchRuntimeService(application.state)
    if args.orchestration_command == "begin-run":
        result = service.begin_run(args.today, args.started_at)
        sys.stdout.write(f"{result.batch_first_started}\n")
    elif args.orchestration_command == "complete-batch-if-exhausted":
        transition = service.complete_batch_if_exhausted(
            args.today,
            args.total,
            args.completed,
        )
        reset = str(transition.reset).lower()
        sys.stdout.write(f"{reset}\t{transition.batch_first_started}\n")
    elif args.orchestration_command == "daily-gate-should-skip":
        should_skip = service.should_skip_daily_gate(
            args.key,
            args.today,
            source_published_today=args.source_published_today == "true",
        )
        return ExitStatus.SUCCESS if should_skip else ExitStatus.NON_FATAL
    elif args.orchestration_command == "complete-daily-gate":
        service.complete_daily_gate(args.key, args.today)
    else:
        raise ValueError(f"unknown orchestration command: {args.orchestration_command}")
    return ExitStatus.SUCCESS


def _update_owners(
    args: argparse.Namespace,
    application: ApplicationContext,
) -> ExitStatus:
    return _operations(application).update_owners(
        OwnerBatchRequest(
            args.since,
            args.batch_marker,
            args.fast_out == "true",
        )
    )


def _operations(
    application: ApplicationContext,
    *,
    progress: Callable[[str], None] | None = None,
    diagnostic: Callable[[str], None] | None = None,
) -> RunApplicationOperations:
    output = LockedRunOutput(progress or _stdout, diagnostic or _stderr)
    return RunApplicationOperations(
        application,
        RunApplicationExecution(
            output.progress,
            output.diagnostic,
            lambda _owners: None,
        ),
    )


def _stdout(message: str) -> None:
    sys.stdout.write(f"{message}\n")
    sys.stdout.flush()


def _stderr(message: str) -> None:
    sys.stderr.write(f"{message}\n")
    sys.stderr.flush()
