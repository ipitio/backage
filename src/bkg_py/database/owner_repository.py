"""Public repository operations for durable owner listing scans."""

from __future__ import annotations

import sqlite3
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from typing import Any

from . import owner_plans, owner_scans
from .models import (
    OwnerRefreshPlan,
    OwnerRefreshSelection,
    OwnerScanCursor,
    OwnerScanFailure,
    OwnerScanPackage,
    OwnerScanPage,
    OwnerScanResult,
    OwnerScanStart,
    OwnerScanWorkSelection,
    PackageRef,
)
from .settings import DatabaseSettings


class OwnerScanRepositoryMixin(ABC):
    """Add typed owner scan operations to the shared SQLite repository."""

    settings: DatabaseSettings

    @abstractmethod
    def ensure_schema(self) -> None:
        """Create or migrate the lazy normalized schema."""

        raise NotImplementedError

    @abstractmethod
    def _run_read(self, operation: Callable[[sqlite3.Connection], Any]) -> Any:
        raise NotImplementedError

    @abstractmethod
    def _run_write(self, operation: Callable[[sqlite3.Connection], Any]) -> Any:
        raise NotImplementedError

    def begin_owner_scan(
        self,
        owner_id: str,
        owner: str,
        marker: str,
        started_at: int,
    ) -> None:
        """Start a fresh resumable owner listing scan."""

        self.ensure_schema()
        self._run_write(
            lambda connection: owner_scans.begin(
                connection, owner_id, owner, marker, started_at
            )
        )

    def current_owner_scan(
        self,
        owner_id: str,
        batch_marker: str,
    ) -> OwnerScanCursor | None:
        """Return the active cursor for this owner and batch, if one exists."""

        self.ensure_schema()
        return self._run_read(
            lambda connection: owner_scans.current(
                connection,
                owner_id,
                batch_marker,
            )
        )

    def begin_or_resume_owner_scan(
        self,
        start: OwnerScanStart,
    ) -> OwnerScanCursor:
        """Resume this batch's scan or transactionally start at page one."""

        self.ensure_schema()
        return self._run_write(
            lambda connection: owner_scans.begin_or_resume(
                connection,
                start,
            )
        )

    def advance_owner_scan_page(self, page: OwnerScanPage) -> None:
        """Advance a scan after all work selected from its current page finishes."""

        self.ensure_schema()
        self._run_write(
            lambda connection: owner_scans.advance_page(
                connection,
                page,
            )
        )

    def owner_scan_active(self, owner_id: str, marker: str) -> bool:
        """Return whether an owner scan marker can be resumed."""

        self.ensure_schema()
        return self._run_read(
            lambda connection: owner_scans.active(connection, owner_id, marker)
        )

    def observe_owner_scan(
        self,
        owner_id: str,
        marker: str,
        packages: Sequence[OwnerScanPackage],
        observed_at: int,
    ) -> None:
        """Persist package identities parsed from one owner listing page."""

        self.ensure_schema()
        self._run_write(
            lambda connection: owner_scans.observe(
                connection, owner_id, marker, packages, observed_at
            )
        )

    def observe_owner_scan_page(
        self,
        page: OwnerScanPage,
        packages: Sequence[OwnerScanPackage],
    ) -> None:
        """Persist one listing page only when it matches the durable cursor."""

        self.ensure_schema()
        self._run_write(
            lambda connection: owner_scans.observe_page(
                connection,
                page,
                packages,
            )
        )

    def owner_scan_packages_needing_refresh(
        self,
        selection: OwnerScanWorkSelection,
    ) -> tuple[OwnerScanPackage, ...]:
        """Return observed packages without current, fully published rows."""

        self.ensure_schema()
        return self._run_read(
            lambda connection: owner_plans.packages_needing_refresh(
                connection,
                self.settings.packages_table,
                selection,
            )
        )

    def owner_refresh_plan(
        self,
        owner_id: str,
        owner: str,
        since: str,
        batch_marker: str = "",
    ) -> OwnerRefreshPlan:
        """Return direct package work and partial-update state for an owner."""

        self.ensure_schema()
        return self._run_read(
            lambda connection: owner_plans.owner_refresh_plan(
                connection,
                self.settings.packages_table,
                OwnerRefreshSelection(owner_id, owner, since, batch_marker),
            )
        )

    def known_owner_type(self, owner_id: str, owner: str) -> str | None:
        """Return an unambiguous owner type from package or active scan state."""

        self.ensure_schema()
        return self._run_read(
            lambda connection: owner_scans.known_owner_type(
                connection,
                self.settings.packages_table,
                owner_id,
                owner,
            )
        )

    def reconcile_owner_scan_package(
        self,
        owner_id: str,
        marker: str,
        package: OwnerScanPackage,
        observed_at: int,
    ) -> tuple[str, ...]:
        """Replace staged aliases with one verified package identity."""

        self.ensure_schema()
        return self._run_write(
            lambda connection: owner_scans.reconcile_package(
                connection,
                owner_id,
                marker,
                package,
                observed_at,
            )
        )

    def missing_owner_scan_packages(
        self,
        owner_id: str,
        marker: str,
    ) -> tuple[PackageRef, ...]:
        """Return known packages absent from the staged owner listing."""

        self.ensure_schema()
        return self._run_read(
            lambda connection: owner_scans.missing(
                connection, owner_id, marker, self.settings.packages_table
            )
        )

    def fail_owner_scan(self, failure: OwnerScanFailure) -> int:
        """Persist owner retry backoff after a failed scan or refresh."""

        self.ensure_schema()
        return self._run_write(
            lambda connection: owner_scans.fail(
                connection,
                failure,
                owner_scans.OwnerRetryPolicy(
                    self.settings.owner_retry_initial_seconds,
                    self.settings.owner_retry_max_seconds,
                ),
            )
        )

    def clear_owner_backoff(
        self,
        owner_id: str,
        owner: str,
        completed_at: int,
    ) -> None:
        """Clear owner retry state after successful direct refresh work."""

        self.ensure_schema()
        self._run_write(
            lambda connection: owner_scans.clear_backoff(
                connection, owner_id, owner, completed_at
            )
        )

    def complete_owner_scan(
        self,
        owner_id: str,
        marker: str,
        scan_date: str,
        completed_at: int,
    ) -> OwnerScanResult:
        """Reconcile one verified complete owner listing scan."""

        self.ensure_schema()
        return self._run_write(
            lambda connection: owner_scans.complete(
                connection,
                owner_scans.OwnerScanCompletion(
                    owner_id,
                    marker,
                    scan_date,
                    completed_at,
                ),
                owner_scans.OwnerScanTables(
                    self.settings.owners_table,
                    self.settings.packages_table,
                    self.settings.versions_table,
                ),
                owner_scans.OwnerRetryPolicy(
                    self.settings.owner_retry_initial_seconds,
                    self.settings.owner_retry_max_seconds,
                ),
            )
        )

    def deferred_owners(self, now: int) -> tuple[tuple[str, int], ...]:
        """Return owners still waiting for their retry time."""

        self.ensure_schema()
        return self._run_read(lambda connection: owner_scans.deferred(connection, now))
