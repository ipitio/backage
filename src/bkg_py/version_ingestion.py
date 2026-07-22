"""Fetch package-version candidates through one pooled GitHub client."""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterator
from dataclasses import dataclass
from typing import Protocol

from .enrichment import (
    RequestCircuit,
    RequestCircuitLease,
    transient_request_error,
)
from .github import GitHubError, GitHubJsonResponse, GitHubTextRequestPolicy
from .version_selection import (
    VersionCandidate,
    VersionCandidatePage,
    VersionSelectionResult,
    VersionSelectionSettings,
    select_version_candidates,
)
from .versions import (
    VersionListingContext,
    package_versions_html_url,
    parse_version_listing_html,
    version_candidates_from_value,
)

_PAGE_SIZE = 30
_API_LISTING_SCOPE = "version-listing-api"
_HTML_LISTING_SCOPE = "version-listing-html"


class VersionIngestionError(RuntimeError):
    """Package-version listings could not be fetched reliably."""


class VersionListingUnavailable(VersionIngestionError):
    """Version listing is paused after repeated transient failures."""


def _ignore_diagnostic(_message: str) -> None:
    pass


@dataclass(frozen=True)
class VersionCandidateLoaderSettings:
    """Stable behavior settings for package-version candidate loading."""

    use_rest_api: bool
    diagnostic: Callable[[str], None] = _ignore_diagnostic


class VersionPageClient(Protocol):
    """HTTP operations required by package-version listing ingestion."""

    def rest_json(self, path: str) -> GitHubJsonResponse:
        """Request one decoded REST response."""

        raise NotImplementedError

    def get_text(
        self,
        url: str,
        *,
        authenticated: bool = False,
        accept: str = "text/html",
        policy: GitHubTextRequestPolicy | None = None,
    ) -> str:
        """Request one text response."""

        raise NotImplementedError


class VersionCandidateLoader:  # pylint: disable=too-few-public-methods
    """Lazily fetch and select candidates for one package."""

    def __init__(
        self,
        client: VersionPageClient,
        context: VersionListingContext,
        settings: VersionCandidateLoaderSettings,
        *,
        request_recovery: RequestCircuit | None = None,
    ) -> None:
        self.client = client
        self.context = context
        self.use_rest_api = settings.use_rest_api
        self.diagnostic = settings.diagnostic
        self.request_recovery = (
            request_recovery if request_recovery is not None else RequestCircuit()
        )

    def select(
        self,
        settings: VersionSelectionSettings,
        *,
        already_updated: Collection[str] = (),
    ) -> VersionSelectionResult:
        """Fetch only the pages needed by the candidate selection policy."""

        return select_version_candidates(
            self._version_pages(),
            self._tagged_pages(),
            settings=settings,
            already_updated=already_updated,
        )

    def _version_pages(self) -> Iterator[VersionCandidatePage]:
        """Yield normal listing pages until GitHub reports the final page."""

        page_number = 1
        while True:
            page = self._load_version_page(page_number)
            yield page
            if not page.has_more:
                return
            page_number += 1

    def _tagged_pages(self) -> Iterator[VersionCandidatePage]:
        """Yield tagged listing pages until GitHub reports the final page."""

        page_number = 1
        while True:
            html = self._get_text(self._tagged_page_url(page_number))
            entries = parse_version_listing_html(html, self.context)
            tag_link_count = html.count("?tag=")
            yield VersionCandidatePage(
                candidates=tuple(entry.candidate() for entry in entries),
                has_more=tag_link_count >= _PAGE_SIZE,
            )
            if tag_link_count < _PAGE_SIZE:
                return
            page_number += 1

    def _load_version_page(self, page_number: int) -> VersionCandidatePage:
        """Load one normal page, preferring REST and falling back to HTML."""

        candidates = self._load_api_page(page_number) if self.use_rest_api else None
        if candidates is None:
            html = self._get_text(self._version_page_url(page_number))
            candidates = tuple(
                entry.candidate()
                for entry in parse_version_listing_html(html, self.context)[:_PAGE_SIZE]
            )
        return VersionCandidatePage(
            candidates=candidates[:_PAGE_SIZE],
            has_more=len(candidates) >= _PAGE_SIZE,
        )

    def _load_api_page(self, page_number: int) -> tuple[VersionCandidate, ...] | None:
        """Return one usable REST page or request the HTML fallback."""

        with self.request_recovery.request(_API_LISTING_SCOPE) as lease:
            if not lease:
                return None
            try:
                response = self.client.rest_json(self._api_page_path(page_number))
            except GitHubError as error:
                cooldown = self._record_failure(lease, error)
                self.diagnostic(
                    f"Version API page {page_number} failed ({error}); "
                    "falling back to HTML"
                )
                if cooldown is not None:
                    self.diagnostic(
                        "Pausing GitHub version-listing API requests for "
                        f"{cooldown:g}s after repeated transient failures; "
                        "using the HTML fallback"
                    )
                return None
            lease.record_success()

        if page_number > 1 and response.value == []:
            return ()

        candidates = version_candidates_from_value(response.value)
        if not candidates or any(
            candidate.version_id == "-1" for candidate in candidates
        ):
            self.diagnostic(
                f"Version API page {page_number} returned unusable data; "
                "falling back to HTML"
            )
            return None
        return candidates

    def _get_text(self, url: str) -> str:
        """Fetch one public HTML page with a package-specific error."""

        with self.request_recovery.request(_HTML_LISTING_SCOPE) as lease:
            if not lease:
                raise VersionListingUnavailable(
                    "GitHub version listing is temporarily paused"
                )
            try:
                html = self.client.get_text(url)
            except GitHubError as error:
                cooldown = self._record_failure(lease, error)
                if cooldown is not None:
                    self.diagnostic(
                        "Pausing GitHub version-listing HTML requests for "
                        f"{cooldown:g}s after repeated transient failures; "
                        "using available data"
                    )
                raise VersionIngestionError(
                    f"failed to fetch version listing for "
                    f"{self.context.owner}/{self.context.package}: {error}"
                ) from error
            lease.record_success()
            return html

    @staticmethod
    def _record_failure(
        lease: RequestCircuitLease,
        error: GitHubError,
    ) -> float | None:
        if transient_request_error(error):
            return lease.record_transient_failure()
        lease.record_success()
        return None

    def _api_page_path(self, page_number: int) -> str:
        """Return the REST path for one package-version page."""

        context = self.context
        return (
            f"{context.owner_type}/{context.owner}/packages/{context.package_type}/"
            f"{context.package}/versions?per_page={_PAGE_SIZE}&page={page_number}"
        )

    def _version_page_url(self, page_number: int) -> str:
        """Return the public HTML URL for one package-version page."""

        return package_versions_html_url(self.context, page_number)

    def _tagged_page_url(self, page_number: int) -> str:
        """Return the tagged-filter HTML URL for one package-version page."""

        return package_versions_html_url(self.context, page_number, tagged=True)
