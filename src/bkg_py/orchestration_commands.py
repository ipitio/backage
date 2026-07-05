"""CLI adapters for Python-owned application orchestration decisions."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING

from .database import DatabaseError
from .discovery import DiscoveryError, OwnerIdentityCache, OwnerIdentityResolver
from .discovery_operations import (
    DiscoveryPhaseExecution,
    DiscoveryPhaseIdentity,
    DiscoveryPhasePaths,
    DiscoveryPhaseRequest,
    DiscoveryPhaseService,
    DiscoveryPhaseServices,
)
from .github import GitHubError
from .orchestration import BatchRuntimeService, RunOutcomePolicy
from .owner_batch import (
    OwnerBatchEffects,
    OwnerBatchExecution,
    OwnerBatchRequest,
    OwnerBatchService,
)
from .owner_operations import OwnerOperationExecution, OwnerUpdateOperation
from .owner_pages import OwnerPageAdmissionConfig, admit_owner_page
from .owner_queue_operations import (
    OwnerQueuePreparationExecution,
    OwnerQueuePreparationPaths,
    OwnerQueuePreparationRequest,
    OwnerQueuePreparationService,
    OwnerQueuePreparationServices,
    TargetedOwnerQueueService,
)
from .result import ExitStatus
from .run_finalization import (
    RunFinalizationExecution,
    RunFinalizationRequest,
    RunFinalizationService,
    RunFinalizationServices,
)
from .run_planning import PackageWorkPlanService
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

        if args.orchestration_command == "prepare-package-plan":
            status = _prepare_package_plan(args, application)
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


def _prepare_package_plan(
    args: argparse.Namespace,
    application: ApplicationContext,
) -> ExitStatus:
    with application.stop.signal_handlers():
        summary = PackageWorkPlanService(application.database).prepare(
            args.since,
            Path(args.directory),
            reset=args.reset == "true",
        )
    sys.stdout.write(f"{summary.total}\t{summary.completed}\t{summary.pending}\n")
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
    config = application.config

    def progress(message: str) -> None:
        sys.stdout.write(f"{message}\n")
        sys.stdout.flush()

    with application.stop.signal_handlers():
        RunFinalizationService(
            RunFinalizationServices(
                application.database,
                application.snapshots,
                RunPublicationService(
                    application.database,
                    application.state,
                    application.stop.check,
                ),
                application.state,
            ),
            RunFinalizationExecution(application.stop.check, progress),
        ).finalize(
            RunFinalizationRequest(
                publication=_publication_request(
                    args,
                    application,
                    rotated=False,
                ),
                optout_file=Path(config.optout_file),
                batch_first_started=(
                    application.state.get("BKG_BATCH_FIRST_STARTED") or args.today
                ),
                prepare_snapshot=args.prepare_snapshot == "true",
                rotation_threshold_bytes=(config.snapshot_rotation_threshold_bytes),
            )
        )
    return ExitStatus.SUCCESS


def _discover_owners(
    args: argparse.Namespace,
    application: ApplicationContext,
) -> ExitStatus:
    config = application.config
    if not application.github_settings.token:
        raise DiscoveryError("authenticated discovery requires GITHUB_TOKEN")

    def progress(message: str) -> None:
        sys.stdout.write(f"{message}\n")
        sys.stdout.flush()

    runtime = BatchRuntimeService(application.state)
    with application.github_client() as client:
        resolver = OwnerIdentityResolver(OwnerIdentityCache.from_config(config), client)
        admission = OwnerPageAdmissionConfig(
            application.state,
            Path(config.owners_file),
            Path(args.packages_all_file),
        )
        service = DiscoveryPhaseService(
            DiscoveryPhaseServices(
                resolver,
                lambda page, per_page: admit_owner_page(
                    resolver,
                    admission,
                    page,
                    per_page,
                ),
                lambda today: runtime.complete_daily_gate(
                    "BKG_LAST_EXPLORE_DATE", today
                ),
            ),
            DiscoveryPhaseExecution(application.stop.check, progress),
        )
        service.run(
            DiscoveryPhaseRequest(
                paths=DiscoveryPhasePaths(
                    Path(args.connections_file),
                    Path(config.owners_file),
                    Path(config.optout_file),
                ),
                identity=DiscoveryPhaseIdentity(
                    config.github_owner,
                    config.github_repo,
                    config.github_owner == "ipitio" and config.mode in {0, 3},
                ),
                today=args.today,
                skip_explore=args.skip_explore == "true",
                first_run=config.is_first != "false" and config.mode in {0, 3},
                owner_page_limit=config.owner_discovery_max_pages,
            )
        )
    return ExitStatus.SUCCESS


def _prepare_targeted_owner_queue(
    args: argparse.Namespace,
    application: ApplicationContext,
    *,
    optouts: bool,
) -> ExitStatus:
    config = application.config

    def progress(message: str) -> None:
        sys.stdout.write(f"{message}\n")
        sys.stdout.flush()

    with application.github_client() as client:
        service = TargetedOwnerQueueService(
            OwnerIdentityResolver(OwnerIdentityCache.from_config(config), client),
            application.state,
            application.stop.check,
            progress,
        )
        if optouts:
            service.prepare_optouts(Path(config.optout_file))
        else:
            service.prepare(config.github_owner, Path(args.connections_file))
    return ExitStatus.SUCCESS


def _prepare_owner_queue(
    args: argparse.Namespace,
    application: ApplicationContext,
) -> ExitStatus:
    config = application.config
    if config.index_dir is None:
        raise ValueError("BKG_INDEX_DIR is required")

    def progress(message: str) -> None:
        sys.stdout.write(f"{message}\n")
        sys.stdout.flush()

    effects = OwnerBatchEffects(
        application.database,
        application.state,
        Path(config.owners_file),
        Path(config.index_dir),
        progress,
    )
    with application.github_client() as client:
        service = OwnerQueuePreparationService(
            OwnerQueuePreparationServices(
                application.database,
                OwnerIdentityResolver(OwnerIdentityCache.from_config(config), client),
                application.state,
                effects.retire_unavailable,
            ),
            OwnerQueuePreparationExecution(
                application.stop.check,
                progress,
            ),
        )
        service.prepare(
            OwnerQueuePreparationRequest(
                paths=OwnerQueuePreparationPaths(
                    connections=Path(args.connections_file),
                    manual_owners=Path(config.owners_file),
                    index_directory=Path(config.index_dir),
                    working_directory=Path(args.working_directory),
                ),
                rest_first=args.rest_first,
                request_limit=args.request_limit,
                current_owner=config.github_owner,
                include_manual=args.include_manual == "true",
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
