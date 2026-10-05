"""Bounded in-process package refreshes for one owner update."""

from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from ..concurrency import BoundedWorkerRunner, ConcurrencySettings
from ..database.models import (
    OwnerScanPackage,
    PackageBatch,
    PackageRef,
)
from ..database.package.repository import PackageRepository
from ..database.support import DatabaseError
from ..github import GitHubError
from ..packages.updates import (
    PackageOptOuts,
    PackageRefreshError,
    PackageRefreshExecution,
    PackageRefreshPolicy,
    PackageRefreshRequest,
    PackageRefreshResult,
    PackageRefreshService,
)
from ..packages.versions.ingestion import VersionPageClient
from ..publication import PublicationError
from ..publication.promotion import PublicationRecoveryError

MessageSink = Callable[[str], None]


class OwnerPackageRefreshError(RuntimeError):
    """An owner package batch could not finish reliably."""


@dataclass(frozen=True)
class OwnerPackageRefreshRequest:
    """Identity, package work, and storage policy for one owner."""

    owner_id: str
    owner: str
    packages: tuple[OwnerScanPackage, ...]
    batch: PackageBatch
    versions_table: str
    index_dir: Path
    policy: PackageRefreshPolicy

    @property
    def since(self) -> str:
        """Return the active batch's package-date cutoff."""

        return self.batch.since

    @property
    def batch_marker(self) -> str:
        """Return the active package refresh generation."""

        return self.batch.marker


@dataclass(frozen=True)
class OwnerPackageRefreshItem:
    """The outcome of one package in an owner refresh batch."""

    package: OwnerScanPackage
    result: PackageRefreshResult | None = None
    error: str = ""

    @property
    def failed(self) -> bool:
        """Return whether this package encountered an expected failure."""

        return bool(self.error)


@dataclass(frozen=True)
class OwnerPackageRefreshResult:
    """Completed package outcomes for one owner refresh batch."""

    items: tuple[OwnerPackageRefreshItem, ...]

    @property
    def failure_count(self) -> int:
        """Return the number of packages left pending after expected failures."""

        return sum(item.failed for item in self.items)

    @property
    def opted_out(self) -> tuple[OwnerScanPackage, ...]:
        """Return exclusions whose file and database cleanup both succeeded."""

        return tuple(
            item.package
            for item in self.items
            if not item.failed
            and item.result is not None
            and item.result.outcome == "opted_out"
        )


@dataclass(frozen=True)
class OwnerPackageRefreshExecution:
    """Shared services and concurrency policy for owner package work."""

    package: PackageRefreshExecution
    concurrency: ConcurrencySettings
    progress: MessageSink
    diagnostic: MessageSink


class OwnerPackageRefreshService:
    """Refresh an owner's packages with one bounded worker budget."""

    def __init__(
        self,
        repository: PackageRepository,
        client: VersionPageClient,
        execution: OwnerPackageRefreshExecution,
    ) -> None:
        self.repository = repository
        self.client = client
        self.execution = execution

    def select_work(
        self,
        request: OwnerPackageRefreshRequest,
        due: tuple[OwnerScanPackage, ...],
    ) -> tuple[OwnerScanPackage, ...]:
        """Include newly excluded identities even when their data is complete."""

        try:
            optouts = PackageOptOuts.load(self.execution.package.optout_file)
        except PackageRefreshError as error:
            raise OwnerPackageRefreshError(str(error)) from error
        if not optouts.entries:
            return due
        excluded = (
            package
            for package in request.packages
            if optouts.matches(self._package_request(request, package).package_ref)
        )
        return tuple(dict.fromkeys((*due, *excluded)))

    def refresh(
        self,
        request: OwnerPackageRefreshRequest,
    ) -> OwnerPackageRefreshResult:
        """Refresh every selected package while preserving expected failures."""

        if not request.packages:
            return OwnerPackageRefreshResult(())
        package_workers, version_workers = allocate_worker_counts(
            len(request.packages),
            self.execution.concurrency.max_workers,
        )
        version_settings = replace(
            self.execution.concurrency,
            max_workers=version_workers,
        )
        package_settings = replace(
            self.execution.concurrency,
            max_workers=package_workers,
        )
        run_result = BoundedWorkerRunner(
            package_settings,
            check_stop=self.execution.package.check_stop,
        ).run(
            request.packages,
            lambda package: self._refresh_one(request, package, version_settings),
            task_name=lambda package: package.package,
        )
        for failure in run_result.failures:
            if isinstance(failure.error, PublicationRecoveryError):
                raise failure.error
        if run_result.stop is not None:
            raise run_result.stop
        if not run_result.ok:
            failure = run_result.failure
            message = (
                str(failure.error) if failure is not None else "worker interrupted"
            )
            raise OwnerPackageRefreshError(
                f"owner package refresh worker failed: {message}"
            )
        return OwnerPackageRefreshResult(
            tuple(completed.value for completed in run_result.completed)
        )

    def _refresh_one(
        self,
        request: OwnerPackageRefreshRequest,
        package: OwnerScanPackage,
        version_settings: ConcurrencySettings,
    ) -> OwnerPackageRefreshItem:
        context = f"{request.owner}/{package.package}"
        self.execution.progress(f"Updating {context}...")
        try:
            result = PackageRefreshService(
                self.repository,
                self.client,
                self._package_execution(version_settings),
            ).refresh(self._package_request(request, package))
        except (
            DatabaseError,
            GitHubError,
            OSError,
            PackageRefreshError,
            PublicationError,
        ) as error:
            message = str(error) or type(error).__name__
            self.execution.diagnostic(
                f"Package refresh failed for {context}: {message}"
            )
            return OwnerPackageRefreshItem(package, error=message)

        self.execution.progress(
            f"Package refresh summary for {context}: {result.json_summary()}"
        )
        if result.outcome == "opted_out":
            self.execution.progress(f"{context} was opted out!")
        elif result.outcome in {"metadata_unavailable", "versions_unavailable"}:
            unavailable = (
                "metadata"
                if result.outcome == "metadata_unavailable"
                else "version inventory"
            )
            self.execution.diagnostic(
                f"Package {unavailable} unavailable for {context}; leaving it pending"
            )
        else:
            self.execution.progress(f"Refreshed {context}")
        return OwnerPackageRefreshItem(package, result=result)

    def _package_execution(
        self,
        version_settings: ConcurrencySettings,
    ) -> PackageRefreshExecution:
        base = self.execution.package
        return replace(
            base,
            version=replace(
                base.version,
                worker_runner=BoundedWorkerRunner(
                    version_settings,
                    check_stop=base.check_stop,
                ),
            ),
        )

    @staticmethod
    def _package_request(
        request: OwnerPackageRefreshRequest,
        package: OwnerScanPackage,
    ) -> PackageRefreshRequest:
        package_ref = PackageRef(
            request.owner_id,
            package.owner_type,
            package.package_type,
            request.owner,
            package.repo,
            package.package,
        )
        legacy_table = (
            f"{request.versions_table}_{package.owner_type}_{package.package_type}_"
            f"{request.owner}_{package.repo}_{package.package}"
        )
        destination = (
            request.index_dir / request.owner / package.repo / f"{package.package}.json"
        )
        return PackageRefreshRequest(
            package_ref,
            legacy_table,
            request.since,
            destination,
            request.policy,
            request.batch_marker,
        )


def allocate_worker_counts(package_count: int, worker_budget: int) -> tuple[int, int]:
    """Split one worker budget across package and version concurrency."""

    if package_count <= 0 or worker_budget <= 0:
        raise ValueError("package count and worker budget must be greater than zero")
    package_workers = min(package_count, worker_budget)
    return package_workers, max(1, worker_budget // package_workers)
