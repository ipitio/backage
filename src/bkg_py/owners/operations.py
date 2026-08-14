"""Execute one complete owner update through caller-composed services."""

from __future__ import annotations

import shutil
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from ..concurrency import ConcurrencySettings
from ..database import (
    DatabaseError,
    OwnerIdentityCleanup,
    OwnerScanCursor,
    OwnerScanFailure,
    OwnerScanPackage,
    PackageBatch,
    PackageRef,
)
from ..discovery import OwnerLookupResult
from ..github import GitHubError
from ..package_discovery import PackageDiscoveryError
from ..package_updates import PackageRefreshPolicy
from ..publication import PublicationError
from ..rendering import RenderingError
from ..runtime_names import legacy_owner_page_key, legacy_owner_scan_key
from .lifecycle import (
    OwnerLifecycleRequest,
    OwnerLifecycleResult,
)
from .package_updates import (
    OwnerPackageRefreshError,
    OwnerPackageRefreshRequest,
)
from .scan_pages import OwnerScanPageError
from .updates import OwnerUpdateError

MessageSink = Callable[[str], None]


class OwnerUpdateRepository(Protocol):
    """Persistence operations required by one outer owner update."""

    def owner_has_aliases(self, owner_id: str, owner: str) -> bool:
        """Return whether an owner has superseded persisted identities."""

        raise NotImplementedError

    def retire_owner_aliases(
        self,
        owner_id: str,
        owner: str,
    ) -> OwnerIdentityCleanup:
        """Reconcile rows for one freshly verified owner identity."""

        raise NotImplementedError

    def known_owner_type(self, owner_id: str, owner: str) -> str | None:
        """Return an already persisted GitHub owner type."""

        raise NotImplementedError

    def current_owner_scan(
        self,
        owner_id: str,
        batch_marker: str,
    ) -> OwnerScanCursor | None:
        """Return the active owner scan cursor, when present."""

        raise NotImplementedError

    def fail_owner_scan(self, failure: OwnerScanFailure) -> int:
        """Persist a retryable failure and return its retry epoch."""

        raise NotImplementedError


class OwnerUpdateState(Protocol):  # pylint: disable=too-few-public-methods
    """Legacy state cleanup required by one owner update."""

    def delete_matching(
        self,
        *,
        keys: Iterable[str] = (),
        prefixes: Iterable[str] = (),
    ) -> set[str]:
        """Delete matching legacy state keys."""

        raise NotImplementedError


class OwnerIdentityLookup(Protocol):
    """Fresh owner identity behavior required by owner operations."""

    def owner_type(self, value: str) -> str | None:
        """Return GitHub's owner type when it exists."""

        raise NotImplementedError

    def resolve_owner_fresh(self, value: str) -> OwnerLookupResult:
        """Resolve an owner without trusting a persisted identity."""

        raise NotImplementedError


class OwnerLifecycle(Protocol):  # pylint: disable=too-few-public-methods
    """One fully composed owner lifecycle."""

    def update(self, request: OwnerLifecycleRequest) -> OwnerLifecycleResult:
        """Run one owner lifecycle."""

        raise NotImplementedError


OwnerLifecycleFactory = Callable[[str], OwnerLifecycle]


@dataclass(frozen=True)
class OwnerUpdateRequest:
    """Identity and run context for one complete owner lifecycle."""

    owner_id: str
    owner: str
    since: str
    batch_marker: str
    today: str
    fast_out: bool = False


@dataclass(frozen=True)
class OwnerOperationExecution:
    """Worker budget and message sinks shared by owner operations."""

    concurrency: ConcurrencySettings
    progress: MessageSink
    diagnostic: MessageSink


@dataclass(frozen=True)
class OwnerUpdateServices:
    """Narrow collaborators supplied by the application composition root."""

    repository: OwnerUpdateRepository
    state: OwnerUpdateState
    identity: OwnerIdentityLookup
    lifecycle_for_date: OwnerLifecycleFactory


@dataclass(frozen=True)
class OwnerUpdatePolicy:
    """Immutable runtime policy needed to build owner package requests."""

    mode: int
    versions_table: str
    index_dir: Path
    use_rest_api: bool


class OwnerUpdateOperation:  # pylint: disable=too-few-public-methods
    """Execute one owner lifecycle using caller-composed services."""

    def __init__(
        self,
        services: OwnerUpdateServices,
        policy: OwnerUpdatePolicy,
        execution: OwnerOperationExecution,
    ) -> None:
        self.services = services
        self.policy = policy
        self.execution = execution

    def update(self, request: OwnerUpdateRequest) -> OwnerLifecycleResult:
        """Run one owner and persist retry backoff for expected failures."""

        try:
            request = self._reconcile_owner_identity(request)
            owner_type = _resolve_owner_api_type(
                request.owner_id,
                request.owner,
                self.services.repository,
                self.services.identity,
                self.execution.progress,
            )
            return self.services.lifecycle_for_date(request.today).update(
                OwnerLifecycleRequest(
                    owner_type,
                    self.policy.mode,
                    _build_package_refresh_request(request, self.policy, ()),
                )
            )
        except (
            DatabaseError,
            GitHubError,
            OSError,
            OwnerPackageRefreshError,
            OwnerScanPageError,
            OwnerUpdateError,
            PackageDiscoveryError,
            PublicationError,
            RenderingError,
        ) as error:
            return _defer_owner_update(
                request,
                self.services.repository,
                self.services.state,
                error,
                self.execution.progress,
            )

    def _reconcile_owner_identity(
        self,
        request: OwnerUpdateRequest,
    ) -> OwnerUpdateRequest:
        has_aliases = self.services.repository.owner_has_aliases(
            request.owner_id,
            request.owner,
        )
        if not has_aliases:
            return request

        resolved = self.services.identity.resolve_owner_fresh(request.owner)
        if resolved.owner_ref is None:
            raise OwnerUpdateError(
                f"could not verify current owner identity for {request.owner}"
            )
        verified_id, separator, verified_owner = resolved.owner_ref.partition("/")
        if not separator or not verified_id.isdecimal() or not verified_owner:
            raise OwnerUpdateError(
                f"GitHub returned an invalid owner identity for {request.owner}"
            )

        cleanup = self.services.repository.retire_owner_aliases(
            verified_id, verified_owner
        )
        self.services.state.delete_matching(
            keys=(
                key
                for alias_id in cleanup.alias_ids
                for key in (
                    legacy_owner_scan_key(alias_id),
                    legacy_owner_page_key(alias_id),
                )
            )
        )
        _remove_orphaned_package_files(
            self.policy.index_dir,
            cleanup.orphaned_packages,
        )
        self.execution.progress(
            f"Reconciled {request.owner} to {verified_owner} owner ID {verified_id}; "
            "retired "
            f"{len(cleanup.alias_ids)} superseded ID(s) and "
            f"{len(cleanup.orphaned_packages)} orphaned package path(s)"
        )
        return replace(request, owner_id=verified_id, owner=verified_owner)


def _remove_orphaned_package_files(
    index_dir: Path,
    packages: tuple[PackageRef, ...],
) -> None:
    for package in packages:
        repo_directory = index_dir / package.owner / package.repo
        if not repo_directory.is_dir():
            continue
        prefixes = (f"{package.package}.json", f"{package.package}.xml")
        for path in repo_directory.iterdir():
            if path.name.startswith(prefixes) and (path.is_file() or path.is_symlink()):
                path.unlink(missing_ok=True)
        if not any(
            path.is_file() and path.suffix == ".json" and not path.name.startswith(".")
            for path in repo_directory.iterdir()
        ):
            shutil.rmtree(repo_directory)


def _build_package_refresh_request(
    request: OwnerUpdateRequest,
    policy: OwnerUpdatePolicy,
    packages: tuple[OwnerScanPackage, ...],
) -> OwnerPackageRefreshRequest:
    """Build one owner package request from typed runtime inputs."""

    return OwnerPackageRefreshRequest(
        request.owner_id,
        request.owner,
        packages,
        PackageBatch(request.since, request.batch_marker),
        policy.versions_table,
        policy.index_dir,
        PackageRefreshPolicy(
            write_legacy=True,
            use_rest_api=policy.use_rest_api,
            fast_out=request.fast_out,
            mode=policy.mode,
        ),
    )


def _resolve_owner_api_type(
    owner_id: str,
    owner: str,
    repository: OwnerUpdateRepository,
    resolver: OwnerIdentityLookup,
    progress: MessageSink,
) -> str:
    known_type = repository.known_owner_type(owner_id, owner)
    if known_type is not None:
        return known_type
    typename = resolver.owner_type(owner)
    if typename == "Organization":
        return "orgs"
    if typename == "User":
        return "users"
    if typename is None:
        progress(f"Owner type unavailable for {owner}; verifying authoritative absence")
        return "users"
    raise OwnerUpdateError(f"unsupported GitHub owner type for {owner}: {typename}")


def _defer_owner_update(
    request: OwnerUpdateRequest,
    repository: OwnerUpdateRepository,
    state: OwnerUpdateState,
    error: Exception,
    progress: MessageSink,
) -> OwnerLifecycleResult:
    cursor = repository.current_owner_scan(
        request.owner_id,
        request.batch_marker,
    )
    message = str(error) or type(error).__name__
    failed_at = int(datetime.now(tz=UTC).timestamp())
    retry_after = repository.fail_owner_scan(
        OwnerScanFailure(
            request.owner_id,
            request.owner,
            cursor.marker if cursor is not None else None,
            message,
            failed_at,
        )
    )
    state.delete_matching(
        keys=(
            legacy_owner_scan_key(request.owner_id),
            legacy_owner_page_key(request.owner_id),
        )
    )
    retry_time = datetime.fromtimestamp(retry_after, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    progress(
        f"Deferred {request.owner} after failed work ({message}) until {retry_time}"
    )
    return OwnerLifecycleResult(
        "deferred",
        retry_after=retry_after,
        error=message,
    )
