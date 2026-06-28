"""Resumable owner package-listing page orchestration."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, replace

from .database import DatabaseRepository
from .database_models import OwnerScanPage, OwnerScanWorkSelection
from .owner_package_updates import (
    OwnerPackageRefreshRequest,
    OwnerPackageRefreshService,
)
from .package_commands import OwnerListingClient, fetch_package_listing_page
from .package_discovery import PackageListingRequest

_MAX_PAGES_PER_RUN = 51

MessageSink = Callable[[str], None]
StopCheck = Callable[[], None]
Clock = Callable[[], int]


class OwnerScanPageError(RuntimeError):
    """An owner page scan request is invalid."""


@dataclass(frozen=True)
class OwnerScanPagesRequest:
    """Inputs for one bounded, resumable owner listing pass."""

    owner_type: str
    marker: str
    start_page: int
    mode: int
    package_refresh: OwnerPackageRefreshRequest
    max_pages: int = _MAX_PAGES_PER_RUN

    def __post_init__(self) -> None:
        if self.start_page < 1:
            raise OwnerScanPageError("owner scan start page must be positive")
        if self.max_pages < 1:
            raise OwnerScanPageError("owner scan page limit must be positive")


@dataclass(frozen=True)
class OwnerScanPagesResult:
    """Durable cursor and owner state after one bounded page pass."""

    next_page: int
    pages_processed: int
    completed: bool = False
    owner_missing: bool = False
    first_page_empty: bool = False
    listing_unavailable: bool = False


@dataclass(frozen=True)
class OwnerScanPageExecution:
    """Runtime hooks shared by one owner page pass."""

    check_stop: StopCheck
    progress: MessageSink
    now: Clock = lambda: int(time.time())


class OwnerScanPageService:  # pylint: disable=too-few-public-methods
    """Fetch, stage, refresh, and advance owner pages with one HTTP client."""

    def __init__(
        self,
        repository: DatabaseRepository,
        client: OwnerListingClient,
        package_refresh: OwnerPackageRefreshService,
        execution: OwnerScanPageExecution,
    ) -> None:
        self.repository = repository
        self.client = client
        self.package_refresh = package_refresh
        self.execution = execution

    def scan(self, request: OwnerScanPagesRequest) -> OwnerScanPagesResult:
        """Process listing pages until the listing or this pass is complete."""

        refresh_request = request.package_refresh
        owner_id = refresh_request.owner_id
        owner = refresh_request.owner
        page_number = request.start_page
        listing_unavailable = False
        first_page_empty = False

        for page_offset in range(request.max_pages):
            self.execution.check_stop()
            self.execution.progress(f"Starting {owner} page {page_number}...")
            fetched = fetch_package_listing_page(
                self.client,
                PackageListingRequest(
                    request.owner_type,
                    owner,
                    page_number,
                    request.mode,
                ),
            )
            page = fetched.page
            self.execution.progress(f"Started {owner} page {page_number}")
            first_page_empty = first_page_empty or (
                page_number == 1 and not page.packages
            )
            if fetched.owner_missing:
                return OwnerScanPagesResult(
                    page_number,
                    page_offset,
                    owner_missing=True,
                    first_page_empty=first_page_empty,
                )

            listing_unavailable = listing_unavailable or fetched.listing_unavailable
            if fetched.listing_unavailable:
                self.execution.progress(
                    f"Package listing unavailable for existing owner "
                    f"{owner}; verifying known packages individually"
                )

            self.repository.observe_owner_scan_page(
                OwnerScanPage(
                    owner_id,
                    request.marker,
                    page_number,
                    self.execution.now(),
                ),
                page.packages,
            )
            work = self.repository.owner_scan_packages_needing_refresh(
                OwnerScanWorkSelection(
                    owner_id,
                    owner,
                    page.packages,
                    refresh_request.since,
                )
            )
            self.package_refresh.refresh(replace(refresh_request, packages=work))
            self.repository.advance_owner_scan_page(
                OwnerScanPage(
                    owner_id,
                    request.marker,
                    page_number,
                    self.execution.now(),
                )
            )
            page_number += 1
            if not page.has_more:
                return OwnerScanPagesResult(
                    page_number,
                    page_offset + 1,
                    completed=True,
                    first_page_empty=first_page_empty,
                    listing_unavailable=listing_unavailable,
                )

        return OwnerScanPagesResult(
            page_number,
            request.max_pages,
            first_page_empty=first_page_empty,
            listing_unavailable=listing_unavailable,
        )
