"""Shell-facing owner update command adapters."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from .database import DatabaseError
from .database_models import OwnerScanPackage
from .github import GitHubError
from .owner_package_updates import (
    OwnerPackageRefreshError,
    OwnerPackageRefreshExecution,
    OwnerPackageRefreshRequest,
    OwnerPackageRefreshService,
)
from .owner_updates import (
    OwnerScanVerificationRequest,
    OwnerScanVerificationService,
    OwnerUpdateError,
)
from .package_updates import PackageRefreshExecution, PackageRefreshPolicy
from .result import ExitStatus
from .runtime import GracefulStop
from .version_updates import DockerManifestInspector, VersionRefreshExecution

if TYPE_CHECKING:
    from .application import ApplicationContext
    from .database_models import OwnerRefreshPlan
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
        "verify-scan",
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
        OwnerUpdateError,
    ) as error:
        print(error, file=sys.stderr)
        return ExitStatus.NON_FATAL

    return ExitStatus.SUCCESS


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
        OwnerPackageRefreshService(
            application.database,
            client,
            OwnerPackageRefreshExecution(
                PackageRefreshExecution(
                    VersionRefreshExecution(
                        application.worker_runner,
                        DockerManifestInspector(application.process_runner),
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
        ).refresh(
            OwnerPackageRefreshRequest(
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


def _package_value(package: OwnerScanPackage) -> dict[str, str]:
    return {
        "owner_type": package.owner_type,
        "package_type": package.package_type,
        "repo": package.repo,
        "package": package.package,
    }
