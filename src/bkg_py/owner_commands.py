"""Shell-facing owner update command adapters."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from .database import DatabaseError
from .database_models import OwnerScanFailure, OwnerScanPackage
from .files import atomic_text_output
from .github import GitHubError
from .owner_lifecycle import (
    OwnerLifecycleExecution,
    OwnerLifecycleRequest,
    OwnerLifecycleResult,
    OwnerLifecycleService,
    OwnerLifecycleServices,
)
from .owner_package_updates import (
    OwnerPackageRefreshError,
    OwnerPackageRefreshExecution,
    OwnerPackageRefreshRequest,
    OwnerPackageRefreshService,
)
from .owner_publication import (
    OwnerPublicationRequest,
    OwnerPublicationResult,
    OwnerPublicationService,
)
from .owner_scan_pages import (
    OwnerScanPageError,
    OwnerScanPageExecution,
    OwnerScanPageService,
    OwnerScanPagesRequest,
)
from .owner_updates import (
    OwnerScanOutcome,
    OwnerScanService,
    OwnerScanVerificationRequest,
    OwnerScanVerificationService,
    OwnerUpdateError,
)
from .package_discovery import PackageDiscoveryError
from .package_updates import PackageRefreshExecution, PackageRefreshPolicy
from .publication import PublicationError
from .registry import GHCRManifestInspector
from .rendering import RenderingError
from .result import ExitStatus
from .runtime import GracefulStop
from .version_updates import VersionRefreshExecution

if TYPE_CHECKING:
    from .application import ApplicationContext
    from .database_models import OwnerRefreshPlan, PackageRef
    from .github import GitHubClient
    from .owner_updates import OwnerScanVerificationResult

_PACKAGE_REF_FIELD_COUNT = 3


def run_owner(
    args: argparse.Namespace,
    application: ApplicationContext,
) -> ExitStatus:
    """Run one owner update operation."""

    if args.owner_command not in {
        "refresh-packages",
        "refresh-plan",
        "scan-pages",
        "verify-scan",
        "publish",
        "update",
    }:
        print(f"unknown owner command: {args.owner_command}", file=sys.stderr)
        return ExitStatus.NON_FATAL
    try:
        with application.stop.signal_handlers():
            if args.owner_command == "refresh-plan":
                plan = application.database.owner_refresh_plan(
                    args.owner_id,
                    args.owner,
                    args.since,
                )
                print(_refresh_plan_json(plan))
            elif args.owner_command == "refresh-packages":
                _refresh_packages(args, application)
            elif args.owner_command == "scan-pages":
                _scan_pages(args, application)
            elif args.owner_command == "publish":
                print(_publication_json(_publish_owner(args, application)))
            elif args.owner_command == "update":
                _update_owner(args, application)
            else:
                with application.github_client() as client:
                    result = OwnerScanVerificationService(
                        application.database,
                        client,
                        application.stop.check,
                    ).verify(
                        OwnerScanVerificationRequest(
                            args.owner_id,
                            args.owner,
                            args.marker,
                            args.since,
                            args.observed_at,
                        )
                    )
                print(_result_json(result))
    except GracefulStop as error:
        reason = str(error) or "requested"
        print(f"Graceful stop requested: {reason}", file=sys.stderr)
        return ExitStatus.GRACEFUL_STOP
    except (
        DatabaseError,
        GitHubError,
        OSError,
        OwnerPackageRefreshError,
        OwnerScanPageError,
        OwnerUpdateError,
        PackageDiscoveryError,
        PublicationError,
        RenderingError,
    ) as error:
        print(error, file=sys.stderr)
        return ExitStatus.NON_FATAL

    return ExitStatus.SUCCESS


def _update_owner(
    args: argparse.Namespace,
    application: ApplicationContext,
) -> None:
    def diagnostic(message: str) -> None:
        print(message, file=sys.stderr)

    def progress(message: str) -> None:
        print(message, flush=True)

    try:
        with application.github_client() as client:
            package_refresh = _package_refresh_service(
                application,
                client,
                progress,
                diagnostic,
            )
            pages = OwnerScanPageService(
                application.database,
                client,
                package_refresh,
                OwnerScanPageExecution(application.stop.check, progress),
            )
            result = OwnerLifecycleService(
                application.database,
                OwnerLifecycleServices(
                    package_refresh,
                    OwnerScanService(
                        application.database,
                        client,
                        pages,
                        package_refresh,
                    ),
                    OwnerPublicationService(
                        application.database,
                        application.aggregate_settings,
                        application.publication_limits,
                        application.stop.check,
                    ),
                ),
                OwnerLifecycleExecution(application.state, progress),
            ).update(
                OwnerLifecycleRequest(
                    args.owner_type,
                    args.batch_marker,
                    application.config.mode,
                    _package_refresh_request(args, application, ()),
                )
            )
    except (
        DatabaseError,
        GitHubError,
        OSError,
        OwnerPackageRefreshError,
        OwnerScanPageError,
        OwnerUpdateError,
        PackageDiscoveryError,
        PublicationError,
        RenderingError,
    ) as error:
        result = _defer_owner_update(args, application, error, progress)

    result_file = Path(args.result_file)
    with atomic_text_output(result_file) as output:
        output.write(_owner_lifecycle_json(result))
        output.write("\n")


def _defer_owner_update(
    args: argparse.Namespace,
    application: ApplicationContext,
    error: Exception,
    progress: Callable[[str], None],
) -> OwnerLifecycleResult:
    cursor = application.database.current_owner_scan(
        args.owner_id,
        args.batch_marker,
    )
    message = str(error) or type(error).__name__
    failed_at = int(datetime.now(tz=UTC).timestamp())
    retry_after = application.database.fail_owner_scan(
        OwnerScanFailure(
            args.owner_id,
            args.owner,
            cursor.marker if cursor is not None else None,
            message,
            failed_at,
        )
    )
    application.state.delete_matching(
        keys=(
            f"BKG_OWNER_SCAN_{args.owner_id}",
            f"BKG_PAGE_{args.owner_id}",
        )
    )
    retry_time = datetime.fromtimestamp(retry_after, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    progress(f"Deferred {args.owner} after failed work ({message}) until {retry_time}")
    return OwnerLifecycleResult(
        "deferred",
        retry_after=retry_after,
        error=message,
    )


def _refresh_packages(
    args: argparse.Namespace,
    application: ApplicationContext,
) -> None:
    index_dir = application.config.index_dir
    if index_dir is None:
        raise OwnerUpdateError("BKG_INDEX_DIR is required")
    packages = _package_refs(args.owner_type, sys.stdin.read())

    def diagnostic(message: str) -> None:
        print(message, file=sys.stderr)

    def progress(message: str) -> None:
        print(message, flush=True)

    with application.github_client() as client:
        _package_refresh_service(
            application,
            client,
            progress,
            diagnostic,
        ).refresh(_package_refresh_request(args, application, packages))


def _scan_pages(
    args: argparse.Namespace,
    application: ApplicationContext,
) -> None:
    def diagnostic(message: str) -> None:
        print(message, file=sys.stderr)

    def progress(message: str) -> None:
        print(message, flush=True)

    with application.github_client() as client:
        package_refresh = _package_refresh_service(
            application,
            client,
            progress,
            diagnostic,
        )
        pages = OwnerScanPageService(
            application.database,
            client,
            package_refresh,
            OwnerScanPageExecution(application.stop.check, progress),
        )
        result = OwnerScanService(
            application.database,
            client,
            pages,
            package_refresh,
        ).scan(
            OwnerScanPagesRequest(
                args.owner_type,
                args.marker,
                args.start_page,
                application.config.mode,
                _package_refresh_request(args, application, ()),
            )
        )
    result_file = Path(args.result_file)
    with atomic_text_output(result_file) as output:
        output.write(_scan_pages_json(result))
        output.write("\n")


def _package_refresh_service(
    application: ApplicationContext,
    client: GitHubClient,
    progress: Callable[[str], None],
    diagnostic: Callable[[str], None],
) -> OwnerPackageRefreshService:
    return OwnerPackageRefreshService(
        application.database,
        client,
        OwnerPackageRefreshExecution(
            PackageRefreshExecution(
                VersionRefreshExecution(
                    application.worker_runner,
                    GHCRManifestInspector(client, diagnostic=diagnostic),
                    diagnostic=diagnostic,
                ),
                application.version_selection_settings,
                application.publication_limits,
                Path(application.config.optout_file),
                application.stop.check,
            ),
            application.concurrency_settings,
            progress,
            diagnostic,
        ),
    )


def _package_refresh_request(
    args: argparse.Namespace,
    application: ApplicationContext,
    packages: tuple[OwnerScanPackage, ...],
) -> OwnerPackageRefreshRequest:
    index_dir = application.config.index_dir
    if index_dir is None:
        raise OwnerUpdateError("BKG_INDEX_DIR is required")
    return OwnerPackageRefreshRequest(
        args.owner_id,
        args.owner,
        packages,
        args.since,
        application.config.versions_table,
        Path(index_dir),
        PackageRefreshPolicy(
            write_legacy=True,
            use_rest_api=bool(application.github_settings.token),
            fast_out=args.fast_out == "true",
            mode=application.config.mode,
        ),
    )


def _publish_owner(
    args: argparse.Namespace,
    application: ApplicationContext,
) -> OwnerPublicationResult:
    index_dir = application.config.index_dir
    if index_dir is None:
        raise OwnerUpdateError("BKG_INDEX_DIR is required")
    return OwnerPublicationService(
        application.database,
        application.aggregate_settings,
        application.publication_limits,
        application.stop.check,
    ).publish(
        OwnerPublicationRequest(
            args.owner_id,
            args.owner,
            Path(index_dir),
        )
    )


def _package_refs(owner_type: str, value: str) -> tuple[OwnerScanPackage, ...]:
    packages: list[OwnerScanPackage] = []
    for line_number, line in enumerate(value.splitlines(), start=1):
        if not line:
            continue
        fields = line.removesuffix("/").split("/", 2)
        if len(fields) != _PACKAGE_REF_FIELD_COUNT or not all(fields):
            raise OwnerUpdateError(
                f"invalid owner package ref on stdin line {line_number}"
            )
        packages.append(OwnerScanPackage(owner_type, *fields))
    return tuple(packages)


def _refresh_plan_json(plan: OwnerRefreshPlan) -> str:
    return json.dumps(
        {
            "partially_updated": plan.partially_updated,
            "pending_count": plan.pending_count,
            "packages": [_package_value(package) for package in plan.packages],
        },
        separators=(",", ":"),
    )


def _publication_json(result: OwnerPublicationResult) -> str:
    return json.dumps(
        {
            "package_count": result.package_count,
            "repositories": list(result.repositories),
        },
        separators=(",", ":"),
    )


def _result_json(result: OwnerScanVerificationResult) -> str:
    return json.dumps(
        {
            "checked_count": result.checked_count,
            "absent_count": result.absent_count,
            "verified_count": len(result.packages),
            "packages": [_package_value(package) for package in result.work],
            "identity_changes": [
                {
                    "package": change.package,
                    "previous_repositories": list(change.previous_repositories),
                    "repository": change.repository,
                }
                for change in result.changes
            ],
        },
        separators=(",", ":"),
    )


def _scan_pages_json(result: OwnerScanOutcome) -> str:
    pages = result.pages
    reconciliation = result.reconciliation
    return json.dumps(
        {
            "next_page": pages.next_page,
            "pages_processed": pages.pages_processed,
            "completed": pages.completed,
            "owner_missing": pages.owner_missing,
            "first_page_empty": pages.first_page_empty,
            "listing_unavailable": pages.listing_unavailable,
            "reconciliation": (
                None
                if reconciliation is None
                else {
                    "checked_count": reconciliation.verification.checked_count,
                    "absent_count": reconciliation.verification.absent_count,
                    "identity_changes": [
                        {
                            "package": change.package,
                            "previous_repositories": list(change.previous_repositories),
                            "repository": change.repository,
                        }
                        for change in reconciliation.verification.changes
                    ],
                    "removed": [
                        _package_ref_value(package)
                        for package in reconciliation.completion.removed
                    ],
                    "pending_count": reconciliation.completion.pending_count,
                    "pending": [
                        _package_value(package)
                        for package in reconciliation.completion.pending
                    ],
                    "retry_after": reconciliation.completion.retry_after,
                }
            ),
        },
        separators=(",", ":"),
    )


def _owner_lifecycle_json(result: OwnerLifecycleResult) -> str:
    pages = result.scan.pages if result.scan is not None else None
    publication = result.publication
    return json.dumps(
        {
            "outcome": result.outcome,
            "next_page": pages.next_page if pages is not None else 0,
            "pages_processed": pages.pages_processed if pages is not None else 0,
            "first_page_empty": (
                pages.first_page_empty if pages is not None else False
            ),
            "listing_unavailable": (
                pages.listing_unavailable if pages is not None else False
            ),
            "empty_owner_recorded": result.empty_owner_recorded,
            "retry_after": result.retry_after,
            "error": result.error,
            "publication": (
                None
                if publication is None
                else {
                    "package_count": publication.package_count,
                    "repositories": list(publication.repositories),
                }
            ),
        },
        separators=(",", ":"),
    )


def _package_value(package: OwnerScanPackage) -> dict[str, str]:
    return {
        "owner_type": package.owner_type,
        "package_type": package.package_type,
        "repo": package.repo,
        "package": package.package,
    }


def _package_ref_value(package: PackageRef) -> dict[str, str]:
    return {
        "owner_id": package.owner_id,
        "owner_type": package.owner_type,
        "package_type": package.package_type,
        "owner": package.owner,
        "repo": package.repo,
        "package": package.package,
    }
