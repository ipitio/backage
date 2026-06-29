"""Owner package-listing verification and identity reconciliation."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Protocol, cast
from urllib.parse import quote

from .database import DatabaseRepository
from .database_models import (
    OwnerScanPackage,
    OwnerScanResult,
    OwnerScanWorkSelection,
    PackageRef,
)
from .github import GitHubJsonResponse
from .owner_package_updates import OwnerPackageRefreshService
from .owner_scan_pages import (
    OwnerScanPageService,
    OwnerScanPagesRequest,
    OwnerScanPagesResult,
)

StopCheck = Callable[[], None]


class OwnerVerificationClient(Protocol):  # pylint: disable=too-few-public-methods
    """GitHub operation required to verify one package identity."""

    def rest_json_optional(self, path: str) -> GitHubJsonResponse | None:
        """Return package metadata or an absent-resource marker."""

        raise NotImplementedError


class OwnerUpdateError(RuntimeError):
    """An owner update could not be verified safely."""


@dataclass(frozen=True)
class OwnerScanVerificationRequest:
    """Inputs for verifying known packages absent from an owner listing."""

    owner_id: str
    owner: str
    marker: str
    since: str
    observed_at: int


@dataclass(frozen=True)
class OwnerScanIdentityChange:
    """A package whose staged repository identity was canonicalized."""

    package: str
    previous_repositories: tuple[str, ...]
    repository: str


@dataclass(frozen=True)
class OwnerScanVerificationResult:
    """Verified identities and package work selected from them."""

    checked_count: int
    absent_count: int
    packages: tuple[OwnerScanPackage, ...]
    work: tuple[OwnerScanPackage, ...]
    changes: tuple[OwnerScanIdentityChange, ...]


@dataclass(frozen=True)
class OwnerScanReconciliation:
    """Verification and database completion for one fully listed owner."""

    verification: OwnerScanVerificationResult
    completion: OwnerScanResult


@dataclass(frozen=True)
class OwnerScanOutcome:
    """Page progress and optional reconciliation from one owner scan pass."""

    pages: OwnerScanPagesResult
    reconciliation: OwnerScanReconciliation | None = None


@dataclass(frozen=True)
class _PackageVerification:
    package: OwnerScanPackage | None
    change: OwnerScanIdentityChange | None = None
    absent_count: int = 0


class OwnerScanVerificationService:  # pylint: disable=too-few-public-methods
    """Verify missing packages and reconcile mutable repository associations."""

    def __init__(
        self,
        repository: DatabaseRepository,
        client: OwnerVerificationClient,
        check_stop: StopCheck,
    ) -> None:
        self.repository = repository
        self.client = client
        self.check_stop = check_stop

    def verify(
        self,
        request: OwnerScanVerificationRequest,
    ) -> OwnerScanVerificationResult:
        """Verify each missing package once and return current-batch work."""

        missing = self.repository.missing_owner_scan_packages(
            request.owner_id,
            request.marker,
        )
        groups = _group_package_aliases(missing)
        verified: list[OwnerScanPackage] = []
        forced_work: set[OwnerScanPackage] = set()
        changes: list[OwnerScanIdentityChange] = []
        absent_count = 0

        for aliases in groups:
            verification = self._verify_aliases(request, aliases)
            absent_count += verification.absent_count
            if verification.package is None:
                continue
            canonical = verification.package
            verified.append(canonical)
            if verification.change is not None:
                forced_work.add(canonical)
                changes.append(verification.change)

        packages = tuple(sorted(set(verified), key=_package_sort_key))
        selected_work = set(
            self.repository.owner_scan_packages_needing_refresh(
                OwnerScanWorkSelection(
                    request.owner_id,
                    request.owner,
                    packages,
                    request.since,
                )
            )
        )
        work = tuple(sorted(selected_work | forced_work, key=_package_sort_key))
        return OwnerScanVerificationResult(
            checked_count=len(groups),
            absent_count=absent_count,
            packages=packages,
            work=work,
            changes=tuple(changes),
        )

    def _verify_aliases(
        self,
        request: OwnerScanVerificationRequest,
        aliases: tuple[PackageRef, ...],
    ) -> _PackageVerification:
        self.check_stop()
        package = aliases[0]
        response = self.client.rest_json_optional(_package_api_path(package))
        if response is None:
            return _PackageVerification(None, absent_count=len(aliases))
        canonical = OwnerScanPackage(
            package.owner_type,
            package.package_type,
            _repository_name(response.value, package.package),
            package.package,
        )
        previous_repositories = self.repository.reconcile_owner_scan_package(
            request.owner_id,
            request.marker,
            canonical,
            request.observed_at,
        )
        previous = tuple(
            repo for repo in previous_repositories if repo != canonical.repo
        )
        change = (
            OwnerScanIdentityChange(canonical.package, previous, canonical.repo)
            if previous
            else None
        )
        return _PackageVerification(canonical, change)


class OwnerScanService:  # pylint: disable=too-few-public-methods
    """Run owner pages through verification and transactional completion."""

    def __init__(
        self,
        repository: DatabaseRepository,
        client: OwnerVerificationClient,
        pages: OwnerScanPageService,
        package_refresh: OwnerPackageRefreshService,
    ) -> None:
        self.repository = repository
        self.client = client
        self.pages = pages
        self.package_refresh = package_refresh

    def scan(self, request: OwnerScanPagesRequest) -> OwnerScanOutcome:
        """Run one bounded pass and reconcile only after its final page."""

        pages = self.pages.scan(request)
        if not pages.completed or pages.owner_missing:
            return OwnerScanOutcome(pages)

        refresh_request = request.package_refresh
        execution = self.pages.execution
        verification = OwnerScanVerificationService(
            self.repository,
            self.client,
            execution.check_stop,
        ).verify(
            OwnerScanVerificationRequest(
                refresh_request.owner_id,
                refresh_request.owner,
                request.marker,
                refresh_request.since,
                execution.now(),
            )
        )
        if verification.changes:
            execution.progress(
                f"Reconciled {len(verification.changes)} package repository "
                f"association(s) for {refresh_request.owner}"
            )
        self.package_refresh.refresh(
            replace(refresh_request, packages=verification.work)
        )
        completion = self.repository.complete_owner_scan(
            refresh_request.owner_id,
            request.marker,
            refresh_request.since,
            execution.now(),
        )
        return OwnerScanOutcome(
            pages,
            OwnerScanReconciliation(verification, completion),
        )


def _group_package_aliases(
    packages: tuple[PackageRef, ...],
) -> tuple[tuple[PackageRef, ...], ...]:
    groups: dict[tuple[str, str, str], list[PackageRef]] = {}
    for package in packages:
        key = (package.owner_type, package.package_type, package.package)
        groups.setdefault(key, []).append(package)
    return tuple(
        tuple(sorted(group, key=lambda package: package.repo))
        for _key, group in sorted(groups.items())
    )


def _package_api_path(package: PackageRef) -> str:
    owner = quote(package.owner, safe="")
    package_name = quote(package.package, safe="%")
    return (
        f"{package.owner_type}/{owner}/packages/{package.package_type}/{package_name}"
    )


def _repository_name(value: object, fallback: str) -> str:
    if not isinstance(value, Mapping):
        raise OwnerUpdateError("package API response must be an object")
    response = cast(Mapping[str, object], value)
    repository = response.get("repository")
    if repository is None:
        return fallback
    if not isinstance(repository, Mapping):
        raise OwnerUpdateError("package API repository must be an object or null")
    repository_value = cast(Mapping[str, object], repository)
    name = repository_value.get("name")
    if not isinstance(name, str) or not name:
        raise OwnerUpdateError("package API repository name must be non-empty")
    return name


def _package_sort_key(package: OwnerScanPackage) -> tuple[str, ...]:
    return (
        package.owner_type,
        package.package_type,
        package.repo,
        package.package,
    )
