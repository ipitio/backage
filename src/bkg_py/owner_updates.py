"""Owner package-listing verification and identity reconciliation."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol, cast
from urllib.parse import quote

from .database import DatabaseRepository
from .database_models import (
    OwnerScanPackage,
    OwnerScanWorkSelection,
    PackageRef,
)
from .github import GitHubJsonResponse

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
