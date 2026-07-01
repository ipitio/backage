"""CLI adapters for Python-owned application orchestration decisions."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING

from .database import DatabaseError
from .orchestration import BatchRuntimeService, RunOutcomePolicy
from .owner_batch import (
    OwnerBatchEffects,
    OwnerBatchExecution,
    OwnerBatchRequest,
    OwnerBatchService,
)
from .owner_operations import OwnerOperationExecution, OwnerUpdateOperation
from .result import ExitStatus
from .run_planning import PackageWorkPlanService
from .runtime import GracefulStop
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

        if args.orchestration_command == "prepare-package-plan":
            return _prepare_package_plan(args, application)

        return _run_batch_runtime(args, application)
    except GracefulStop as error:
        reason = str(error) or "requested"
        sys.stderr.write(f"Graceful stop requested: {reason}\n")
        return ExitStatus.GRACEFUL_STOP
    except (DatabaseError, OSError, StateValueError, ValueError) as error:
        sys.stderr.write(f"{error}\n")
        return ExitStatus.NON_FATAL


def _owner_phase_decision(args: argparse.Namespace) -> ExitStatus:
    decision = RunOutcomePolicy.owner_updates(
        args.phase_status,
        args.run_status,
    )
    sys.stdout.write(f"{decision.action}\t{decision.run_status}\t{decision.message}\n")
    return ExitStatus.SUCCESS


def _prepare_package_plan(
    args: argparse.Namespace,
    application: ApplicationContext,
) -> ExitStatus:
    with application.stop.signal_handlers():
        summary = PackageWorkPlanService(application.database).prepare(
            args.since,
            Path(args.directory),
        )
    sys.stdout.write(f"{summary.total}\t{summary.completed}\t{summary.pending}\n")
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
            args.remaining,
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
    index_dir = application.config.index_dir
    if index_dir is None:
        raise ValueError("BKG_INDEX_DIR is required")
    output_lock = Lock()

    def progress(message: str) -> None:
        with output_lock:
            sys.stdout.write(f"{message}\n")
            sys.stdout.flush()

    def diagnostic(message: str) -> None:
        with output_lock:
            sys.stderr.write(f"{message}\n")
            sys.stderr.flush()

    with application.github_client() as client:
        service = OwnerBatchService(
            lambda concurrency: (
                OwnerUpdateOperation(
                    application,
                    client,
                    OwnerOperationExecution(
                        concurrency,
                        progress,
                        diagnostic,
                    ),
                ).update
            ),
            OwnerBatchEffects(
                application.database,
                application.state,
                Path(application.config.owners_file),
                Path(index_dir),
                progress,
            ),
            OwnerBatchExecution(
                application.state,
                Path(application.config.optout_file),
                application.concurrency_settings,
                application.stop.check,
                progress,
                diagnostic,
            ),
        )
        return service.run(
            OwnerBatchRequest(
                args.since,
                args.batch_marker,
                args.fast_out == "true",
            )
        )
