"""Detailed package-version inspection and transactional refresh orchestration."""

from __future__ import annotations

import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import unquote

from .concurrency import BoundedWorkerRunner
from .database import DatabaseRepository
from .database_models import PackageRef, VersionMetrics, VersionRecord, VersionStage
from .github import GitHubError
from .runtime import CommandOptions, GracefulStop, ProcessRunner
from .version_ingestion import VersionCandidateLoader, VersionPageClient
from .version_selection import (
    VersionCandidate,
    VersionSelectionResult,
    VersionSelectionSettings,
)
from .versions import (
    ManifestSizeResult,
    VersionListingContext,
    extract_oci_version_labels,
    extract_version_page_data,
    manifest_size,
    package_detail_html_url,
    package_version_detail_html_url,
)

_BADGE_SIZE_PATTERN = re.compile(r">([0-9]+(?:\.[0-9]+)?)\s*([^<]*)<")
_SIZE_PATTERN = re.compile(r"^([0-9]+(?:\.[0-9]+)?)\s*([^\s]*)$")
_SIZE_PREFIXES = "kMGTPEZY"

DiagnosticSink = Callable[[str], None]
ManifestInspector = Callable[[str], str]


def _utc_today() -> str:
    return datetime.now(UTC).date().isoformat()


def _ignore_diagnostic(_message: str) -> None:
    pass


class VersionRefreshError(RuntimeError):
    """A package-version refresh could not complete reliably."""


@dataclass(frozen=True)
class VersionRefreshRequest:
    """Package identity and compatibility settings for one version refresh."""

    package_ref: PackageRef
    legacy_table: str
    write_legacy: bool
    use_rest_api: bool
    since: str = "0000-00-00"
    mark_publication_pending: bool = False

    @property
    def listing_context(self) -> VersionListingContext:
        """Return the GitHub listing identity for this package."""

        package = self.package_ref
        return VersionListingContext(
            owner_type=package.owner_type,
            owner=package.owner,
            repo=package.repo,
            package_type=package.package_type,
            package=package.package,
        )


@dataclass(frozen=True)
class VersionRefreshResult:
    """Candidate selection and persistence outcome for one package."""

    selection: VersionSelectionResult
    records_written: int


@dataclass(frozen=True)
class VersionRefreshExecution:
    """Worker and fallback policy shared by one version refresh."""

    worker_runner: BoundedWorkerRunner
    manifest_inspector: ManifestInspector
    diagnostic: DiagnosticSink = _ignore_diagnostic
    today: Callable[[], str] = _utc_today


@dataclass(frozen=True)
class VersionDetailExecution:
    """Manifest, authentication, and diagnostics for version detail requests."""

    manifest_inspector: ManifestInspector
    authenticated: bool = False
    diagnostic: DiagnosticSink = _ignore_diagnostic


class DockerManifestInspector:  # pylint: disable=too-few-public-methods
    """Best-effort stop-aware adapter around ``docker manifest inspect``."""

    def __init__(self, runner: ProcessRunner) -> None:
        self.runner = runner

    def __call__(self, reference: str) -> str:
        try:
            result = self.runner.run(
                ("docker", "manifest", "inspect", "-v", reference),
                options=CommandOptions(combine_output=True),
            )
        except OSError:
            return ""
        if result.returncode != 0:
            return ""
        return result.stdout.decode(errors="replace")


class VersionDetailInspector:  # pylint: disable=too-few-public-methods
    """Turn one selected GitHub package version into a database record."""

    def __init__(
        self,
        client: VersionPageClient,
        context: VersionListingContext,
        execution: VersionDetailExecution,
    ) -> None:
        self.client = client
        self.context = context
        self.inspect_manifest = execution.manifest_inspector
        self.authenticated = execution.authenticated
        self.diagnostic = execution.diagnostic
        self._badge_lock = threading.Lock()
        self._badge_size: int | None = None

    def inspect(self, candidate: VersionCandidate, *, today: str) -> VersionRecord:
        """Fetch and normalize one candidate's current metrics and size."""

        html = self._get_optional_text(
            self._detail_url(candidate.version_id),
            authenticated=self.authenticated,
        )
        page_data = extract_version_page_data(html)
        tags = candidate.tags
        size = -1

        if self.context.package_type == "container":
            embedded = manifest_size(page_data.manifest)
            self._report_manifest_fallback(
                embedded,
                f"{self.context.owner}/{self.context.package}/"
                f"{candidate.version_id} embedded manifest",
            )
            size = embedded.size
            if not tags:
                tags = extract_oci_version_labels(page_data.manifest)

            if size < 0:
                reference = self._manifest_reference(candidate.name)
                inspected_manifest = self.inspect_manifest(reference)
                inspected = manifest_size(inspected_manifest)
                self._report_manifest_fallback(
                    inspected,
                    f"{reference} inspected manifest",
                )
                size = inspected.size

            if size < 0:
                size = self._package_badge_size()

        return VersionRecord(
            version_id=candidate.version_id,
            name=candidate.name,
            metrics=VersionMetrics(
                size=size,
                downloads=page_data.metrics.total,
                downloads_month=page_data.metrics.month,
                downloads_week=page_data.metrics.week,
                downloads_day=page_data.metrics.day,
            ),
            date=today,
            tags=",".join(_unique_nonempty(tags)),
        )

    def _get_optional_text(self, url: str, *, authenticated: bool = False) -> str:
        try:
            return self.client.get_text(url, authenticated=authenticated)
        except GitHubError as error:
            self.diagnostic(f"Version detail request failed for {url}: {error}")
            return ""

    def _detail_url(self, version_id: str) -> str:
        if version_id == "-1":
            return package_detail_html_url(self.context)
        return package_version_detail_html_url(self.context, version_id)

    def _manifest_reference(self, version_name: str) -> str:
        owner = self.context.owner.lower()
        package = unquote(self.context.package).lower()
        separator = "@" if version_name.startswith("sha256:") else ":"
        return f"ghcr.io/{owner}/{package}{separator}{version_name}"

    def _package_badge_size(self) -> int:
        with self._badge_lock:
            if self._badge_size is not None:
                return self._badge_size
            context = self.context
            html = self._get_optional_text(
                f"https://ghcr-badge.egpl.dev/{context.owner}/{context.package}/size"
            )
            self._badge_size = parse_badge_size(html)
            return self._badge_size

    def _report_manifest_fallback(
        self,
        result: ManifestSizeResult,
        context: str,
    ) -> None:
        fallback_reason = result.fallback_reason
        if fallback_reason is None:
            return
        summary = result.diagnostic_summary
        suffix = f"; {summary}" if summary else ""
        self.diagnostic(
            f"Unable to derive container size from {context}: {fallback_reason}{suffix}"
        )


class VersionRefreshService:  # pylint: disable=too-few-public-methods
    """Select, inspect, and persist one package's version refresh."""

    def __init__(
        self,
        repository: DatabaseRepository,
        client: VersionPageClient,
        execution: VersionRefreshExecution,
    ) -> None:
        self.repository = repository
        self.client = client
        self.execution = execution

    def refresh(
        self,
        request: VersionRefreshRequest,
        settings: VersionSelectionSettings,
    ) -> VersionRefreshResult:
        """Run a complete package-version refresh through one Python process."""

        self.repository.ensure_schema()
        existing = self.repository.version_rows(
            request.package_ref,
            since=request.since,
            legacy_table=request.legacy_table,
        )
        selection = VersionCandidateLoader(
            self.client,
            request.listing_context,
            use_rest_api=request.use_rest_api,
            diagnostic=self.execution.diagnostic,
        ).select(
            settings,
            already_updated={row.version_id for row in existing.rows},
        )
        inspector = VersionDetailInspector(
            self.client,
            request.listing_context,
            VersionDetailExecution(
                self.execution.manifest_inspector,
                authenticated=request.use_rest_api,
                diagnostic=self.execution.diagnostic,
            ),
        )
        today = self.execution.today()
        run_result = self.execution.worker_runner.run(
            selection.candidates,
            lambda candidate: inspector.inspect(candidate, today=today),
            task_name=lambda candidate: candidate.version_id,
        )
        stage = VersionStage(
            package_ref=request.package_ref,
            legacy_table=request.legacy_table,
            write_legacy=request.write_legacy,
            rows=tuple(result.value for result in run_result.completed),
        )

        if run_result.stopped:
            self.repository.finalize_version_stage(
                stage,
                publication_pending_at=(
                    today if request.mark_publication_pending else None
                ),
            )
            stop = next(
                failure.error
                for failure in run_result.failures
                if isinstance(failure.error, GracefulStop)
            )
            raise stop

        records_written = self.repository.flush_version_stage(
            stage,
            publication_pending_at=(
                today if request.mark_publication_pending else None
            ),
        )
        if not run_result.ok:
            failure = run_result.failure
            message = (
                str(failure.error) if failure is not None else "worker interrupted"
            )
            raise VersionRefreshError(f"version detail update failed: {message}")

        return VersionRefreshResult(selection, records_written)


def parse_size_value(value: str) -> int:
    """Convert the human-readable size forms accepted by the shell fallback."""

    match = _SIZE_PATTERN.fullmatch(value.strip())
    if match is None:
        return -1
    try:
        size = Decimal(match.group(1))
    except InvalidOperation:
        return -1

    unit = match.group(2)
    if unit:
        prefix = unit[0]
        exponent = _SIZE_PREFIXES.find(prefix) + 1
        if exponent > 0:
            if unit.endswith("iB"):
                multiplier = 1024
            elif unit.endswith("B"):
                multiplier = 1000
            elif unit.endswith("b"):
                multiplier = 125
            else:
                multiplier = 1024
            size *= Decimal(multiplier) ** exponent
    return int(size)


def parse_badge_size(html: str) -> int:
    """Return the final numeric size advertised by a ghcr-badge response."""

    matches = _BADGE_SIZE_PATTERN.findall(html)
    if not matches:
        return -1
    number, unit = matches[-1]
    return parse_size_value(f"{number} {unit.strip()}")


def _unique_nonempty(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value.strip() for value in values if value.strip()))
