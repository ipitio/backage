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
    from .database_models import OwnerScanPackage
    from .owner_updates import OwnerScanVerificationResult


def run_owner(
    args: argparse.Namespace,
    application: ApplicationContext,
) -> ExitStatus:
    """Run one owner update operation."""

    if args.owner_command != "verify-scan":
        print(f"unknown owner command: {args.owner_command}", file=sys.stderr)
        return ExitStatus.NON_FATAL
    try:
        with application.stop.signal_handlers(), application.github_client() as client:
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
    except GracefulStop as error:
        reason = str(error) or "requested"
        print(f"Graceful stop requested: {reason}", file=sys.stderr)
        return ExitStatus.GRACEFUL_STOP
    except (DatabaseError, GitHubError, OwnerUpdateError) as error:
        print(error, file=sys.stderr)
        return ExitStatus.NON_FATAL

    print(_result_json(result))
    return ExitStatus.SUCCESS


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
