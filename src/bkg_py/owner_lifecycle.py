"""Inner owner update lifecycle built from Python-owned domain services."""

from __future__ import annotations

import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Literal, Protocol

from .database import DatabaseRepository
from .database_models import (
    OwnerRecord,
    OwnerScanCursor,
    OwnerScanResult,
    OwnerScanStart,
)
from .owner_package_updates import (
    OwnerPackageRefreshRequest,
    OwnerPackageRefreshResult,
)
from .owner_publication import (
    OwnerPublicationRequest,
    OwnerPublicationResult,
)
from .owner_scan_pages import OwnerScanPagesRequest
from .owner_updates import OwnerScanOutcome, OwnerUpdateError
from .state import StateStore

MessageSink = Callable[[str], None]
Clock = Callable[[], int]
OwnerLifecycleOutcome = Literal["updated", "paused", "missing", "deferred"]
_PENDING_SUMMARY_LIMIT = 10


class OwnerPackageRefresher(Protocol):  # pylint: disable=too-few-public-methods
    """Package batch behavior required by the owner lifecycle."""

    def refresh(
        self,
        request: OwnerPackageRefreshRequest,
    ) -> OwnerPackageRefreshResult:
        """Refresh selected package work."""

        raise NotImplementedError


class OwnerScanner(Protocol):  # pylint: disable=too-few-public-methods
    """Owner scan behavior required by the owner lifecycle."""

    def scan(self, request: OwnerScanPagesRequest) -> OwnerScanOutcome:
        """Run one bounded owner scan pass."""

        raise NotImplementedError


class OwnerPublisher(Protocol):  # pylint: disable=too-few-public-methods
    """Aggregate publication behavior required by the owner lifecycle."""

    def publish(self, request: OwnerPublicationRequest) -> OwnerPublicationResult:
        """Publish owner and repository aggregates."""

        raise NotImplementedError


@dataclass(frozen=True)
class OwnerLifecycleServices:
    """Collaborating services used by one owner lifecycle."""

    package_refresh: OwnerPackageRefresher
    scanner: OwnerScanner
    publisher: OwnerPublisher


@dataclass(frozen=True)
class OwnerLifecycleRequest:
    """Configuration for one resumable inner owner update."""

    owner_type: str
    batch_marker: str
    mode: int
    package_refresh: OwnerPackageRefreshRequest


@dataclass(frozen=True)
class OwnerLifecycleResult:
    """Outcome consumed by the outer owner policy."""

    outcome: OwnerLifecycleOutcome
    scan: OwnerScanOutcome | None = None
    publication: OwnerPublicationResult | None = None
    empty_owner_recorded: bool = False
    retry_after: int = 0
    error: str = ""


@dataclass(frozen=True)
class OwnerLifecycleExecution:
    """Persisted state and runtime hooks shared through one owner update."""

    state: StateStore
    progress: MessageSink
    now: Clock = lambda: int(time.time())


class OwnerLifecycleService:  # pylint: disable=too-few-public-methods
    """Coordinate direct refresh, resumable scanning, and publication."""

    def __init__(
        self,
        repository: DatabaseRepository,
        services: OwnerLifecycleServices,
        execution: OwnerLifecycleExecution,
    ) -> None:
        self.repository = repository
        self.services = services
        self.execution = execution

    def update(self, request: OwnerLifecycleRequest) -> OwnerLifecycleResult:
        """Run one owner until completion, pause, or authoritative absence."""

        refresh_request = request.package_refresh
        plan = self.repository.owner_refresh_plan(
            refresh_request.owner_id,
            refresh_request.owner,
            refresh_request.since,
        )
        cursor: OwnerScanCursor | None = None
        if plan.has_current_data:
            cursor = self._current_scan(request)
            if cursor is None:
                self.services.package_refresh.refresh(
                    replace(refresh_request, packages=plan.packages)
                )
                plan = self.repository.owner_refresh_plan(
                    refresh_request.owner_id,
                    refresh_request.owner,
                    refresh_request.since,
                )
                if plan.pending_count == 0:
                    self.repository.clear_owner_backoff(
                        refresh_request.owner_id,
                        refresh_request.owner,
                        self.execution.now(),
                    )
                    return self._publish(request)
                self.execution.progress(
                    f"{refresh_request.owner} has {plan.pending_count} unresolved "
                    "package refresh(es); verifying the complete owner listing"
                )

        cursor = cursor or self._begin_or_resume_scan(request)
        scan = self.services.scanner.scan(
            OwnerScanPagesRequest(
                request.owner_type,
                cursor.marker,
                cursor.next_page,
                request.mode,
                refresh_request,
            )
        )
        if scan.pages.owner_missing:
            self._clear_legacy_scan_state(refresh_request.owner_id)
            return OwnerLifecycleResult("missing", scan=scan)
        if not scan.pages.completed:
            return OwnerLifecycleResult("paused", scan=scan)
        if scan.reconciliation is None:
            raise OwnerUpdateError("completed owner scan has no reconciliation result")

        completion = scan.reconciliation.completion
        self._clear_legacy_scan_state(refresh_request.owner_id)
        self._remove_reconciled_files(request, completion)
        self._report_pending(refresh_request.owner, completion)
        return self._publish(request, scan=scan)

    def _current_scan(self, request: OwnerLifecycleRequest) -> OwnerScanCursor | None:
        refresh = request.package_refresh
        cursor = self.repository.current_owner_scan(
            refresh.owner_id,
            request.batch_marker,
        )
        marker_key, page_key = _legacy_scan_keys(refresh.owner_id)
        legacy_marker = self.execution.state.get(marker_key)
        discarded = bool(
            legacy_marker and (cursor is None or legacy_marker != cursor.marker)
        )
        if cursor is None or discarded:
            self.execution.state.delete_matching(keys=(marker_key, page_key))
        if discarded:
            self.execution.progress(
                f"Discarding stale owner scan marker for {refresh.owner}; "
                "database state is authoritative"
            )
        return cursor

    def _begin_or_resume_scan(self, request: OwnerLifecycleRequest) -> OwnerScanCursor:
        refresh = request.package_refresh
        marker_key, page_key = _legacy_scan_keys(refresh.owner_id)
        legacy_marker = self.execution.state.get(marker_key)
        legacy_page_value = self.execution.state.get_int(page_key)
        cursor = self.repository.begin_or_resume_owner_scan(
            OwnerScanStart(
                refresh.owner_id,
                refresh.owner,
                request.batch_marker,
                self.execution.now(),
                legacy_marker,
                legacy_page_value if legacy_page_value > 0 else None,
            )
        )
        self.execution.state.delete_matching(keys=(marker_key, page_key))
        if legacy_marker and legacy_marker != cursor.marker:
            self.execution.progress(
                f"Discarding stale owner scan marker for {refresh.owner}; "
                "database state is authoritative"
            )
        return cursor

    def _publish(
        self,
        request: OwnerLifecycleRequest,
        *,
        scan: OwnerScanOutcome | None = None,
    ) -> OwnerLifecycleResult:
        refresh = request.package_refresh
        self.execution.progress(f"Creating {refresh.owner} arrays...")
        publication = self.services.publisher.publish(
            OwnerPublicationRequest(
                refresh.owner_id,
                refresh.owner,
                refresh.index_dir,
            )
        )
        empty_recorded = False
        if (
            publication.package_count == 0
            and refresh.since != "0000-00-00"
            and f"{refresh.owner_id}/{refresh.owner}"
            in self.execution.state.get_set("BKG_DISCOVERED_CONNECTION_OWNERS")
        ):
            self.repository.write_owner(
                OwnerRecord(refresh.owner_id, refresh.owner, refresh.since)
            )
            empty_recorded = True
        return OwnerLifecycleResult(
            "updated",
            scan=scan,
            publication=publication,
            empty_owner_recorded=empty_recorded,
        )

    def _remove_reconciled_files(
        self,
        request: OwnerLifecycleRequest,
        completion: OwnerScanResult,
    ) -> None:
        refresh = request.package_refresh
        for package in completion.removed:
            repo_directory = refresh.index_dir / refresh.owner / package.repo
            if not repo_directory.is_dir():
                continue
            prefixes = (
                f"{package.package}.json",
                f"{package.package}.xml",
            )
            for path in repo_directory.iterdir():
                if path.name.startswith(prefixes) and (
                    path.is_file() or path.is_symlink()
                ):
                    path.unlink(missing_ok=True)
            if not any(
                path.is_file()
                and path.suffix == ".json"
                and not path.name.startswith(".")
                for path in repo_directory.iterdir()
            ):
                shutil.rmtree(repo_directory)

    def _report_pending(self, owner: str, completion: OwnerScanResult) -> None:
        if completion.pending_count == 0:
            return
        names = [f"{package.repo}/{package.package}" for package in completion.pending]
        summary = ", ".join(names[:_PENDING_SUMMARY_LIMIT])
        if len(names) > _PENDING_SUMMARY_LIMIT:
            summary += ", ..."
        retry_at = datetime.fromtimestamp(completion.retry_after, UTC).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        self.execution.progress(
            f"Deferred {owner} with {completion.pending_count} incomplete package "
            f"refresh(es) ({summary}) until {retry_at}"
        )

    def _clear_legacy_scan_state(self, owner_id: str) -> None:
        self.execution.state.delete_matching(keys=_legacy_scan_keys(owner_id))


def _legacy_scan_keys(owner_id: str) -> tuple[str, str]:
    return f"BKG_OWNER_SCAN_{owner_id}", f"BKG_PAGE_{owner_id}"
