"""Shell-facing owner update command adapters."""

from __future__ import annotations

import argparse
import json
import sys
from typing import TYPE_CHECKING

from .database import DatabaseError
from .github import GitHubError
from .owner_updates import (
    OwnerScanVerificationRequest,
    OwnerScanVerificationService,
    OwnerUpdateError,
)
from .result import ExitStatus
from .runtime import GracefulStop

if TYPE_CHECKING:
    from .application import ApplicationContext
    from .database_models import OwnerRefreshPlan, OwnerScanPackage
    from .owner_updates import OwnerScanVerificationResult


def run_owner(
    args: argparse.Namespace,
    application: ApplicationContext,
) -> ExitStatus:
    """Run one owner update operation."""

    if args.owner_command not in {"refresh-plan", "verify-scan"}:
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
    except (DatabaseError, GitHubError, OwnerUpdateError) as error:
        print(error, file=sys.stderr)
        return ExitStatus.NON_FATAL

    return ExitStatus.SUCCESS


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
