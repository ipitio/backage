"""Package metadata refresh, persistence, and generated-file publication."""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .database import DatabaseError, DatabaseRepository
from .database_models import PackageRecord, PackageRef, VersionRecord
from .github import GitHubError, GitHubNotFoundError
from .publication import (
    PublicationError,
    PublicationLimits,
    PublicationResult,
    publish_json_file,
)
from .rendering import PackageRenderOptions, RenderingError, render_package_file
from .version_ingestion import VersionIngestionError, VersionPageClient
from .version_selection import VersionSelectionSettings
from .version_updates import (
    VersionRefreshError,
    VersionRefreshExecution,
    VersionRefreshRequest,
    VersionRefreshResult,
    VersionRefreshService,
)
from .versions import (
    DownloadMetrics,
    VersionListingContext,
    extract_download_metrics,
    package_detail_html_url,
)

DiagnosticSink = Callable[[str], None]
StopCheck = Callable[[], None]


class PackageRefreshError(RuntimeError):
    """One package could not be committed and published reliably."""


@dataclass(frozen=True)
class PackageRefreshPolicy:
    """Compatibility switches controlling one package refresh."""

    write_legacy: bool
    use_rest_api: bool
    fast_out: bool
    mode: int


@dataclass(frozen=True)
class PackageRefreshRequest:
    """Identity, storage, and policy for one package refresh."""

    package_ref: PackageRef
    legacy_table: str
    since: str
    destination: Path
    policy: PackageRefreshPolicy


@dataclass(frozen=True)
class PackageRefreshExecution:
    """Shared runtime services used by one package refresh."""

    version: VersionRefreshExecution
    selection: VersionSelectionSettings
    publication_limits: PublicationLimits
    optout_file: Path
    check_stop: StopCheck


@dataclass(frozen=True)
class PackageRefreshResult:
    """Persistence and publication outcome for one package."""

    outcome: str
    package_written: bool = False
    version_refresh: VersionRefreshResult | None = None
    publication: PublicationResult | None = None

    def json_summary(self) -> str:
        """Return the compact shell-facing refresh summary."""

        return json.dumps(
            {
                "outcome": self.outcome,
                "package_written": self.package_written,
                "records_written": (
                    0
                    if self.version_refresh is None
                    else self.version_refresh.records_written
                ),
                "json_size": (
                    0 if self.publication is None else self.publication.json_size
                ),
                "xml_size": (
                    0 if self.publication is None else self.publication.xml_size
                ),
            },
            separators=(",", ":"),
        )


@dataclass(frozen=True)
class PackageOptOuts:
    """Literal and component-regex package exclusions from ``optout.txt``."""

    entries: tuple[str, ...]

    @classmethod
    def load(cls, path: Path) -> PackageOptOuts:
        """Read non-empty exclusions, treating an absent file as empty."""

        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return cls(())
        except (OSError, UnicodeError) as error:
            raise PackageRefreshError(f"failed to read {path}: {error}") from error
        return cls(tuple(line.strip() for line in lines if line.strip()))

    def matches(self, package: PackageRef) -> bool:
        """Return whether any owner, repository, or package exclusion matches."""

        return any(_optout_matches(entry, package) for entry in self.entries)


class PackageRefreshService:  # pylint: disable=too-few-public-methods
    """Refresh one package and publish files from its committed database state."""

    def __init__(
        self,
        repository: DatabaseRepository,
        client: VersionPageClient,
        execution: PackageRefreshExecution,
    ) -> None:
        self.repository = repository
        self.client = client
        self.execution = execution

    def refresh(self, request: PackageRefreshRequest) -> PackageRefreshResult:
        """Run package metadata, version, database, and publication work."""

        self.execution.check_stop()
        package = request.package_ref
        if PackageOptOuts.load(self.execution.optout_file).matches(package):
            self.repository.retire_package(package)
            _remove_package_files(request.destination)
            return PackageRefreshResult("opted_out")
        if request.policy.fast_out:
            return PackageRefreshResult("fast_out")

        today = self.execution.version.today()
        already_updated = self.repository.package_updated_since(package, request.since)
        version_result: VersionRefreshResult | None = None
        advertised_metrics: DownloadMetrics | None = None
        if not already_updated:
            advertised_metrics = self._package_metrics(
                package,
                authenticated=request.policy.use_rest_api,
            )
            if advertised_metrics is None:
                return PackageRefreshResult("metadata_unavailable")
            version_result = self._refresh_versions(request, today)

        source = self.repository.version_rows(
            package,
            since=request.since,
            legacy_table=request.legacy_table,
        )
        package_record = _package_record(
            package,
            source.rows,
            advertised_metrics,
            previous_downloads=self.repository.maximum_package_downloads(package),
            today=today,
        )
        package_written = not already_updated or request.policy.mode == 1
        if package_written:
            self.repository.write_package_pending_publication(package_record)
        else:
            self.repository.mark_package_publication_pending(package, today)

        publication = self._publish(request, today, has_versions=bool(source.rows))
        self.repository.cleanup_legacy_package(
            package,
            request.legacy_table,
            since=request.since,
        )
        self.repository.clear_package_publication(package)
        self._verify_publication(request, publication, today)
        return PackageRefreshResult(
            "refreshed",
            package_written=package_written,
            version_refresh=version_result,
            publication=publication,
        )

    def _package_metrics(
        self,
        package: PackageRef,
        *,
        authenticated: bool,
    ) -> DownloadMetrics | None:
        context = _listing_context(package)
        url = package_detail_html_url(context)
        try:
            html = self.client.get_text(url, authenticated=authenticated)
        except GitHubNotFoundError:
            return None
        except GitHubError as error:
            self.execution.version.diagnostic(
                f"Package detail request failed for {url}: {error}"
            )
            return None
        if "Total downloads" not in html:
            self.execution.version.diagnostic(
                f"Package detail page has no download metrics for "
                f"{package.owner}/{package.package}"
            )
            return None
        return extract_download_metrics(html)

    def _refresh_versions(
        self,
        request: PackageRefreshRequest,
        today: str,
    ) -> VersionRefreshResult | None:
        execution = self.execution.version
        try:
            return VersionRefreshService(
                self.repository,
                self.client,
                VersionRefreshExecution(
                    execution.worker_runner,
                    execution.manifest_inspector,
                    diagnostic=execution.diagnostic,
                    today=lambda: today,
                ),
            ).refresh(
                VersionRefreshRequest(
                    request.package_ref,
                    request.legacy_table,
                    request.policy.write_legacy,
                    request.policy.use_rest_api,
                    request.since,
                    mark_publication_pending=True,
                ),
                self.execution.selection,
            )
        except (
            DatabaseError,
            GitHubError,
            OSError,
            VersionIngestionError,
            VersionRefreshError,
        ) as error:
            package = request.package_ref
            execution.diagnostic(
                f"Version refresh failed for {package.owner}/{package.package}; "
                f"using available data: {error}"
            )
            return None

    def _publish(
        self,
        request: PackageRefreshRequest,
        today: str,
        *,
        has_versions: bool,
    ) -> PublicationResult:
        destination = request.destination
        destination.parent.mkdir(parents=True, exist_ok=True)
        _remove_legacy_sidecars(destination)
        descriptor, staged_name = tempfile.mkstemp(
            dir=destination.parent,
            prefix=f".{destination.name}.",
        )
        os.close(descriptor)
        staged = Path(staged_name)
        staged.unlink()
        try:
            render_package_file(
                self.repository,
                request.package_ref,
                staged,
                PackageRenderOptions(
                    since=request.since,
                    output_date=today,
                    version_limit=-1,
                    legacy_table=request.legacy_table,
                ),
                self.execution.check_stop,
            )
            if not has_versions:
                package = request.package_ref
                self.execution.version.diagnostic(
                    f"No version rows available for {package.owner}/{package.package}; "
                    "using package-level fallback data"
                )
            return publish_json_file(
                staged,
                self.execution.check_stop,
                self.execution.publication_limits,
                destination,
            )
        except (OSError, PublicationError, RenderingError) as error:
            raise PackageRefreshError(str(error)) from error
        finally:
            staged.unlink(missing_ok=True)

    def _verify_publication(
        self,
        request: PackageRefreshRequest,
        publication: PublicationResult,
        today: str,
    ) -> None:
        package = request.package_ref
        problems: list[str] = []
        if not self.repository.package_updated_since(package, request.since):
            problems.append("current package row missing")
        if self.repository.package_publication_pending(package):
            problems.append("publication marker still pending")

        expected_files = (
            (request.destination, publication.json_size),
            (request.destination.with_suffix(".xml"), publication.xml_size),
        )
        for path, expected_size in expected_files:
            try:
                actual_size = path.stat().st_size
            except FileNotFoundError:
                problems.append(f"missing {path.name}")
                continue
            if actual_size != expected_size:
                problems.append(
                    f"{path.name} size {actual_size} does not match {expected_size}"
                )

        if not problems:
            return
        self.repository.mark_package_publication_pending(package, today)
        identity = f"{package.owner}/{package.repo}/{package.package}"
        raise PackageRefreshError(
            f"package publication verification failed for {identity}: "
            f"{'; '.join(problems)}"
        )


def _package_record(
    package: PackageRef,
    versions: tuple[VersionRecord, ...],
    advertised: DownloadMetrics | None,
    *,
    previous_downloads: int,
    today: str,
) -> PackageRecord:
    newest_sized = max(
        (version for version in versions if version.metrics.size > 1),
        key=lambda version: (_numeric_version_id(version.version_id), version.date),
        default=None,
    )
    size = -1 if newest_sized is None else newest_sized.metrics.size
    downloads = -1 if advertised is None else advertised.total
    downloads = max(
        downloads,
        previous_downloads,
        _metric_sum(versions, lambda version: version.metrics.downloads),
    )
    return PackageRecord(
        package_ref=package,
        downloads=downloads,
        downloads_month=_metric_sum(
            versions,
            lambda version: version.metrics.downloads_month,
        ),
        downloads_week=_metric_sum(
            versions,
            lambda version: version.metrics.downloads_week,
        ),
        downloads_day=_metric_sum(
            versions,
            lambda version: version.metrics.downloads_day,
        ),
        size=size,
        date=today,
    )


def _metric_sum(
    versions: tuple[VersionRecord, ...],
    metric: Callable[[VersionRecord], int],
) -> int:
    if not versions:
        return -1
    total = sum(metric(version) for version in versions)
    return total if total >= 0 else -1


def _numeric_version_id(value: str) -> int:
    return int(value) if value.isdecimal() else 0


def _listing_context(package: PackageRef) -> VersionListingContext:
    return VersionListingContext(
        owner_type=package.owner_type,
        owner=package.owner,
        repo=package.repo,
        package_type=package.package_type,
        package=package.package,
    )


def _optout_matches(entry: str, package: PackageRef) -> bool:
    if entry.startswith("/"):
        components = re.sub(r"/(?=/)", "\n", entry).splitlines()
    else:
        components = entry.split("/", maxsplit=2)
    targets = (package.owner, package.repo, package.package)
    if not components or len(components) > len(targets):
        return False
    return all(
        _optout_component_matches(component, target)
        for component, target in zip(
            components,
            targets[: len(components)],
            strict=True,
        )
    )


def _optout_component_matches(component: str, target: str) -> bool:
    if not component.startswith("/"):
        return component == target
    try:
        return re.search(component.removeprefix("/"), target) is not None
    except re.error:
        return False


def _remove_package_files(destination: Path) -> None:
    if not destination.parent.is_dir():
        return
    prefix = f"{destination.name.removesuffix('.json')}."
    for path in destination.parent.iterdir():
        if not path.name.startswith(prefix):
            continue
        if path.is_file() or path.is_symlink():
            path.unlink(missing_ok=True)


def _remove_legacy_sidecars(destination: Path) -> None:
    if not destination.parent.is_dir():
        return
    prefixes = tuple(f"{destination.name}.{suffix}" for suffix in ("abs", "rel", "tmp"))
    for path in destination.parent.iterdir():
        if path.name.startswith(prefixes):
            path.unlink(missing_ok=True)
