"""Shell-facing package discovery and refresh command adapters."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol
from urllib.parse import quote

from . import package_updates, version_updates
from .database import DatabaseError
from .database_models import (
    OwnerScanCursor,
    OwnerScanPackage,
    OwnerScanPage,
    OwnerScanStart,
    OwnerScanWorkSelection,
    PackageRef,
    load_owner_scan_packages,
)
from .github import GitHubError, GitHubJsonResponse, GitHubNotFoundError
from .package_discovery import (
    PackageDiscoveryError,
    PackageListingClient,
    PackageListingPage,
    PackageListingRequest,
    PackageListingService,
)
from .package_updates import PackageRefreshError
from .publication import PublicationError
from .registry import GHCRBadgeSizeInspector, GHCRManifestInspector
from .result import ExitStatus
from .runtime import GracefulStop
from .state import StateValueError

if TYPE_CHECKING:
    from .application import ApplicationContext
    from .package_updates import PackageRefreshResult


class OwnerListingClient(
    PackageListingClient,
    Protocol,
):  # pylint: disable=too-few-public-methods
    """GitHub operations used to classify a missing package listing."""

    def rest_json_optional(self, path: str) -> GitHubJsonResponse | None:
        """Return owner metadata or an absent-resource marker."""

        raise NotImplementedError


def run_package(
    args: argparse.Namespace,
    application: ApplicationContext,
) -> ExitStatus:
    """Run one package discovery or refresh operation."""

    if args.package_command in {
        "active-scan",
        "begin-scan",
        "finish-page",
        "observe-refs",
    }:
        return _run_package_scan(args, application)
    if args.package_command == "list-page":
        return _run_package_listing(args, application)
    if args.package_command == "refresh":
        return _run_package_refresh(args, application)
    print(f"unknown package command: {args.package_command}", file=sys.stderr)
    return ExitStatus.NON_FATAL


@dataclass(frozen=True)
class PackageListingWork:
    """A parsed listing page and the subset requiring package work."""

    page: PackageListingPage
    packages: tuple[OwnerScanPackage, ...]
    owner_missing: bool = False
    listing_unavailable: bool = False


@dataclass(frozen=True)
class PackageListingFetch:
    """One listing page classified against the owner's current existence."""

    page: PackageListingPage
    owner_missing: bool = False
    listing_unavailable: bool = False


def _run_package_listing(
    args: argparse.Namespace,
    application: ApplicationContext,
) -> ExitStatus:
    try:
        listing = _execute_package_listing(args, application)
    except GracefulStop as error:
        return _graceful_stop_status(error)
    except (DatabaseError, GitHubError, OSError, PackageDiscoveryError) as error:
        print(error, file=sys.stderr)
        return ExitStatus.NON_FATAL

    print(
        json.dumps(
            {
                "packages": [
                    {
                        "owner_type": package.owner_type,
                        "package_type": package.package_type,
                        "repo": package.repo,
                        "package": package.package,
                    }
                    for package in listing.packages
                ],
                "observed_count": len(listing.page.packages),
                "has_more": listing.page.has_more,
                "owner_missing": listing.owner_missing,
                "listing_unavailable": listing.listing_unavailable,
            },
            separators=(",", ":"),
        )
    )
    return ExitStatus.SUCCESS


def _execute_package_listing(
    args: argparse.Namespace,
    application: ApplicationContext,
) -> PackageListingWork:
    request = PackageListingRequest(
        args.owner_type,
        args.owner,
        args.page,
        application.config.mode,
    )
    with application.stop.signal_handlers(), application.github_client() as client:
        fetched = fetch_package_listing_page(client, request)
        page = fetched.page
        if args.marker != "-":
            application.database.observe_owner_scan_page(
                OwnerScanPage(
                    args.owner_id,
                    args.marker,
                    args.page,
                    args.observed_at,
                ),
                page.packages,
            )
            packages = application.database.owner_scan_packages_needing_refresh(
                OwnerScanWorkSelection(
                    args.owner_id,
                    args.owner,
                    page.packages,
                    args.since,
                    application.state.get("BKG_BATCH_MARKER") or "",
                )
            )
        else:
            packages = page.packages
    return PackageListingWork(
        page,
        packages,
        fetched.owner_missing,
        fetched.listing_unavailable,
    )


def fetch_package_listing_page(
    client: OwnerListingClient,
    request: PackageListingRequest,
) -> PackageListingFetch:
    """Fetch a listing and confirm whether a 404 means its owner is absent."""

    try:
        return PackageListingFetch(PackageListingService(client).fetch(request))
    except GitHubNotFoundError:
        owner_path = f"{request.owner_type}/{quote(request.owner, safe='')}"
        if client.rest_json_optional(owner_path) is None:
            return PackageListingFetch(PackageListingPage((), False), True)
        return PackageListingFetch(
            PackageListingPage((), False),
            listing_unavailable=True,
        )


def _run_package_scan(
    args: argparse.Namespace,
    application: ApplicationContext,
) -> ExitStatus:
    try:
        with application.stop.signal_handlers():
            if args.package_command == "active-scan":
                _print_active_scan(args, application)
            elif args.package_command == "begin-scan":
                _print_begun_scan(args, application)
            elif args.package_command == "finish-page":
                application.database.advance_owner_scan_page(
                    OwnerScanPage(
                        args.owner_id,
                        args.marker,
                        args.page,
                        args.completed_at,
                    )
                )
            else:
                _print_observed_work(args, application)
    except GracefulStop as error:
        return _graceful_stop_status(error)
    except (DatabaseError, OSError, StateValueError) as error:
        print(error, file=sys.stderr)
        return ExitStatus.NON_FATAL
    return ExitStatus.SUCCESS


def _legacy_scan_keys(owner_id: str) -> tuple[str, str]:
    return f"BKG_OWNER_SCAN_{owner_id}", f"BKG_PAGE_{owner_id}"


def _cursor_value(cursor: OwnerScanCursor) -> dict[str, str | int | bool]:
    return {
        "marker": cursor.marker,
        "next_page": cursor.next_page,
        "resumed": cursor.resumed,
    }


def _print_active_scan(
    args: argparse.Namespace,
    application: ApplicationContext,
) -> None:
    cursor = application.database.current_owner_scan(
        args.owner_id,
        args.batch_marker,
    )
    marker_key, page_key = _legacy_scan_keys(args.owner_id)
    legacy_marker = application.state.get(marker_key)
    discarded_legacy = bool(
        legacy_marker and (cursor is None or legacy_marker != cursor.marker)
    )
    if cursor is None or discarded_legacy:
        application.state.delete_matching(keys=(marker_key, page_key))
    value: dict[str, str | int | bool] = {
        "active": cursor is not None,
        "discarded_legacy": discarded_legacy,
    }
    if cursor is not None:
        value.update(_cursor_value(cursor))
    print(json.dumps(value, separators=(",", ":")))


def _print_begun_scan(
    args: argparse.Namespace,
    application: ApplicationContext,
) -> None:
    marker_key, page_key = _legacy_scan_keys(args.owner_id)
    legacy_marker = application.state.get(marker_key)
    legacy_page_value = application.state.get_int(page_key)
    legacy_page = legacy_page_value if legacy_page_value > 0 else None
    cursor = application.database.begin_or_resume_owner_scan(
        OwnerScanStart(
            args.owner_id,
            args.owner,
            args.batch_marker,
            args.started_at,
            legacy_marker,
            legacy_page,
        )
    )
    application.state.delete_matching(keys=(marker_key, page_key))
    value = _cursor_value(cursor)
    value["discarded_legacy"] = bool(legacy_marker and legacy_marker != cursor.marker)
    print(json.dumps(value, separators=(",", ":")))


def _print_observed_work(
    args: argparse.Namespace,
    application: ApplicationContext,
) -> None:
    packages = load_owner_scan_packages(Path(args.packages_file))
    application.database.observe_owner_scan(
        args.owner_id,
        args.marker,
        packages,
        args.observed_at,
    )
    for package in application.database.owner_scan_packages_needing_refresh(
        OwnerScanWorkSelection(
            args.owner_id,
            args.owner,
            packages,
            args.since,
            application.state.get("BKG_BATCH_MARKER") or "",
        )
    ):
        print(f"{package.package_type}/{package.repo}/{package.package}")


def _run_package_refresh(
    args: argparse.Namespace,
    application: ApplicationContext,
) -> ExitStatus:
    index_dir = application.config.index_dir
    if index_dir is None:
        print("BKG_INDEX_DIR is required", file=sys.stderr)
        return ExitStatus.NON_FATAL

    try:
        result = _execute_package_refresh(args, application, Path(index_dir))
    except GracefulStop as error:
        return _graceful_stop_status(error)
    except (
        DatabaseError,
        GitHubError,
        OSError,
        PackageRefreshError,
        PublicationError,
    ) as error:
        print(error, file=sys.stderr)
        return ExitStatus.NON_FATAL

    print(result.json_summary())
    return ExitStatus.SUCCESS


def _execute_package_refresh(
    args: argparse.Namespace,
    application: ApplicationContext,
    index_dir: Path,
) -> PackageRefreshResult:
    package = _package_ref(args)
    destination = index_dir / package.owner / package.repo / f"{package.package}.json"

    def diagnostic(message: str) -> None:
        print(message, file=sys.stderr)

    with application.stop.signal_handlers(), application.github_client() as client:
        return package_updates.PackageRefreshService(
            application.database,
            client,
            package_updates.PackageRefreshExecution(
                version=version_updates.VersionRefreshExecution(
                    application.worker_runner,
                    GHCRManifestInspector(client, diagnostic=diagnostic),
                    diagnostic=diagnostic,
                    metric_enrichment=application.metric_enrichment,
                    hosted_size_inspector=GHCRBadgeSizeInspector(
                        client,
                        application.metric_enrichment,
                        diagnostic=diagnostic,
                    ),
                ),
                selection=application.version_selection_settings,
                publication_limits=application.publication_limits,
                optout_file=Path(application.config.optout_file),
                check_stop=application.stop.check,
            ),
        ).refresh(
            package_updates.PackageRefreshRequest(
                package,
                args.legacy_table,
                args.since,
                destination,
                package_updates.PackageRefreshPolicy(
                    _boolean_argument(args.write_legacy),
                    _boolean_argument(args.use_rest_api),
                    _boolean_argument(args.fast_out),
                    application.config.mode,
                ),
                application.state.get("BKG_BATCH_MARKER") or "",
            )
        )


def _package_ref(args: argparse.Namespace) -> PackageRef:
    return PackageRef(
        owner_id=args.owner_id,
        owner_type=args.owner_type,
        package_type=args.package_type,
        owner=args.owner,
        repo=args.repo,
        package=args.package,
    )


def _boolean_argument(value: str) -> bool:
    return value == "true"


def _graceful_stop_status(error: GracefulStop) -> ExitStatus:
    reason = str(error) or "requested"
    print(f"Graceful stop requested: {reason}", file=sys.stderr)
    return ExitStatus.GRACEFUL_STOP
