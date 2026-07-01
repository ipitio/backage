"""Reusable construction and execution of one complete owner update."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .application import ApplicationContext
from .concurrency import BoundedWorkerRunner, ConcurrencySettings
from .database import DatabaseError, DatabaseRepository
from .database_models import OwnerScanFailure, OwnerScanPackage
from .discovery import OwnerIdentityCache, OwnerIdentityResolver
from .github import GitHubClient, GitHubError
from .owner_lifecycle import (
    OwnerLifecycleExecution,
    OwnerLifecycleRequest,
    OwnerLifecycleResult,
    OwnerLifecycleService,
    OwnerLifecycleServices,
)
from .owner_package_updates import (
    OwnerPackageRefreshError,
    OwnerPackageRefreshExecution,
    OwnerPackageRefreshRequest,
    OwnerPackageRefreshService,
)
from .owner_publication import OwnerPublicationService
from .owner_scan_pages import (
    OwnerScanPageError,
    OwnerScanPageExecution,
    OwnerScanPageService,
)
from .owner_updates import OwnerScanService, OwnerUpdateError
from .package_discovery import PackageDiscoveryError
from .package_updates import PackageRefreshExecution, PackageRefreshPolicy
from .publication import PublicationError
from .registry import GHCRManifestInspector
from .rendering import RenderingError
from .version_updates import VersionRefreshExecution

MessageSink = Callable[[str], None]


@dataclass(frozen=True)
class OwnerUpdateRequest:
    """Identity and run context for one complete owner lifecycle."""

    owner_id: str
    owner: str
    since: str
    batch_marker: str
    fast_out: bool = False


@dataclass(frozen=True)
class OwnerOperationExecution:
    """Worker budget and message sinks shared by owner operations."""

    concurrency: ConcurrencySettings
    progress: MessageSink
    diagnostic: MessageSink


class OwnerUpdateOperation:  # pylint: disable=too-few-public-methods
    """Build and execute one owner lifecycle with shared process services."""

    def __init__(
        self,
        application: ApplicationContext,
        client: GitHubClient,
        execution: OwnerOperationExecution,
    ) -> None:
        self.application = application
        self.client = client
        self.execution = execution
        self.identity = OwnerIdentityResolver(
            OwnerIdentityCache.from_config(application.config),
            client,
        )

    def update(self, request: OwnerUpdateRequest) -> OwnerLifecycleResult:
        """Run one owner and persist retry backoff for expected failures."""

        try:
            owner_type = _resolve_owner_api_type(
                request.owner_id,
                request.owner,
                self.application.database,
                self.identity,
                self.execution.progress,
            )
            package_refresh = build_package_refresh_service(
                self.application,
                self.client,
                self.execution.concurrency,
                self.execution.progress,
                self.execution.diagnostic,
            )
            pages = OwnerScanPageService(
                self.application.database,
                self.client,
                package_refresh,
                OwnerScanPageExecution(
                    self.application.stop.check,
                    self.execution.progress,
                ),
            )
            return OwnerLifecycleService(
                self.application.database,
                OwnerLifecycleServices(
                    package_refresh,
                    OwnerScanService(
                        self.application.database,
                        self.client,
                        pages,
                        package_refresh,
                    ),
                    OwnerPublicationService(
                        self.application.database,
                        self.application.aggregate_settings,
                        self.application.publication_limits,
                        self.application.stop.check,
                    ),
                ),
                OwnerLifecycleExecution(
                    self.application.state,
                    self.execution.progress,
                ),
            ).update(
                OwnerLifecycleRequest(
                    owner_type,
                    request.batch_marker,
                    self.application.config.mode,
                    build_package_refresh_request(request, self.application, ()),
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
                self.application,
                error,
                self.execution.progress,
            )


def build_package_refresh_service(
    application: ApplicationContext,
    client: GitHubClient,
    concurrency: ConcurrencySettings,
    progress: MessageSink,
    diagnostic: MessageSink,
) -> OwnerPackageRefreshService:
    """Build package refresh behavior using a caller-provided worker budget."""

    return OwnerPackageRefreshService(
        application.database,
        client,
        OwnerPackageRefreshExecution(
            PackageRefreshExecution(
                VersionRefreshExecution(
                    BoundedWorkerRunner(
                        concurrency,
                        check_stop=application.stop.check,
                    ),
                    GHCRManifestInspector(client, diagnostic=diagnostic),
                    diagnostic=diagnostic,
                    metric_enrichment=application.metric_enrichment,
                ),
                application.version_selection_settings,
                application.publication_limits,
                Path(application.config.optout_file),
                application.stop.check,
            ),
            concurrency,
            progress,
            diagnostic,
        ),
    )


def build_package_refresh_request(
    request: OwnerUpdateRequest,
    application: ApplicationContext,
    packages: tuple[OwnerScanPackage, ...],
) -> OwnerPackageRefreshRequest:
    """Build one owner package request from typed runtime inputs."""

    index_dir = application.config.index_dir
    if index_dir is None:
        raise OwnerUpdateError("BKG_INDEX_DIR is required")
    return OwnerPackageRefreshRequest(
        request.owner_id,
        request.owner,
        packages,
        request.since,
        application.config.versions_table,
        Path(index_dir),
        PackageRefreshPolicy(
            write_legacy=True,
            use_rest_api=bool(application.github_settings.token),
            fast_out=request.fast_out,
            mode=application.config.mode,
        ),
    )


def _resolve_owner_api_type(
    owner_id: str,
    owner: str,
    repository: DatabaseRepository,
    resolver: OwnerIdentityResolver,
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
    application: ApplicationContext,
    error: Exception,
    progress: MessageSink,
) -> OwnerLifecycleResult:
    cursor = application.database.current_owner_scan(
        request.owner_id,
        request.batch_marker,
    )
    message = str(error) or type(error).__name__
    failed_at = int(datetime.now(tz=UTC).timestamp())
    retry_after = application.database.fail_owner_scan(
        OwnerScanFailure(
            request.owner_id,
            request.owner,
            cursor.marker if cursor is not None else None,
            message,
            failed_at,
        )
    )
    application.state.delete_matching(
        keys=(
            f"BKG_OWNER_SCAN_{request.owner_id}",
            f"BKG_PAGE_{request.owner_id}",
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
