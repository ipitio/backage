"""Fetch package-version candidates through one pooled GitHub client."""

from collections.abc import Callable, Collection, Iterator
from dataclasses import dataclass
from typing import Protocol, cast
from urllib.parse import unquote, urlencode

from ...github import GitHubError, GitHubJsonResponse, GitHubTextRequestPolicy
from ..enrichment import (
    RequestCircuit,
    RequestCircuitLease,
    transient_request_error,
)
from .metadata import (
    VersionListingContext,
    package_versions_html_url,
    parse_version_listing_html,
    version_candidates_from_value,
)
from .selection import (
    VersionCandidate,
    VersionCandidatePage,
    VersionSelectionResult,
    VersionSelectionSettings,
    select_version_candidates,
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

        if self.context.source_package_id:
            self._verify_legacy_identity()
        return select_version_candidates(
            self._version_pages(),
            self._tagged_pages(),
            settings=settings,
            already_updated=already_updated,
        )

    def _verify_legacy_identity(self) -> None:
        """Never join a repository-scoped package to another package's versions."""

        path = self._api_package_path()
        with self.request_recovery.request(_API_LISTING_SCOPE) as lease:
            if not lease:
                raise VersionListingUnavailable(
                    "GitHub version listing is temporarily paused"
                )
            try:
                response = self.client.rest_json(path)
            except GitHubError as error:
                self._record_failure(lease, error)
                raise VersionListingUnavailable(
                    f"Repository-scoped package identity unavailable for {path}: "
                    f"{error}"
                ) from error
            lease.record_success()
        if not _matching_legacy_identity(response.value, self.context):
            raise VersionListingUnavailable(
                f"Repository-scoped package identity mismatch for {path}; "
                "retaining package for retry"
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

        if self.context.source_package_id:
            return
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

        legacy_route = bool(self.context.source_package_id)
        candidates = (
            self._load_api_page(page_number)
            if self.use_rest_api or legacy_route
            else None
        )
        if candidates is None and legacy_route:
            raise VersionListingUnavailable(
                f"Repository-scoped version inventory unavailable for "
                f"{self.context.owner}/{self.context.package}; "
                "HTML exposes version names but not stable numeric IDs"
            )
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
                    + self._api_fallback_description()
                )
                if cooldown is not None:
                    self.diagnostic(
                        "Pausing GitHub version-listing API requests for "
                        f"{cooldown:g}s after repeated transient failures; "
                        + self._api_fallback_description()
                    )
                return None
            lease.record_success()

        if page_number > 1 and response.value == []:
            return ()

        candidates = version_candidates_from_value(response.value)
        if self.context.source_package_id and not _usable_legacy_candidates(
            response.value, candidates
        ):
            self.diagnostic(
                f"Version API page {page_number} returned unusable data; "
                + self._api_fallback_description()
            )
            return None
        if not candidates or any(
            candidate.version_id == "-1" for candidate in candidates
        ):
            self.diagnostic(
                f"Version API page {page_number} returned unusable data; "
                "falling back to HTML"
            )
            return None
        return candidates

    def _api_fallback_description(self) -> str:
        return (
            "retaining repository-scoped package for retry"
            if self.context.source_package_id
            else "falling back to HTML"
        )

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

        query = urlencode({"per_page": _PAGE_SIZE, "page": page_number})
        return f"{self._api_package_path()}/versions?{query}"

    def _api_package_path(self) -> str:
        """Return the canonical REST package identity path."""

        context = self.context
        return (
            f"{context.owner_type}/{context.owner}/packages/{context.package_type}/"
            f"{context.package}"
        )

    def _version_page_url(self, page_number: int) -> str:
        """Return the public HTML URL for one package-version page."""

        return package_versions_html_url(self.context, page_number)

    def _tagged_page_url(self, page_number: int) -> str:
        """Return the tagged-filter HTML URL for one package-version page."""

        return package_versions_html_url(self.context, page_number, tagged=True)


def _usable_legacy_candidates(
    value: object, candidates: tuple[VersionCandidate, ...]
) -> bool:
    """Do not substitute anonymous records for authoritative Maven versions."""

    if not isinstance(value, list):
        return False
    for item in cast(list[object], value):
        if not isinstance(item, dict):
            return False
        name = cast(dict[str, object], item).get("name")
        if not isinstance(name, str) or not name:
            return False
    return (
        bool(candidates)
        and len({item.version_id for item in candidates}) == len(candidates)
        and all(
            item.version_id.isascii()
            and item.version_id.isdigit()
            and int(item.version_id) > 0
            for item in candidates
        )
    )


def _matching_legacy_identity(value: object, context: VersionListingContext) -> bool:
    """Require matching numeric identity, ecosystem, coordinates, and repository."""

    if not isinstance(value, dict):
        return False
    package = cast(dict[str, object], value)
    package_id = package.get("id")
    if isinstance(package_id, bool) or str(package_id) != context.source_package_id:
        return False
    if package.get("package_type") != context.package_type or package.get(
        "name"
    ) != unquote(context.package):
        return False
    repository = package.get("repository")
    if not isinstance(repository, dict):
        return False
    full_name = cast(dict[str, object], repository).get("full_name")
    return (
        isinstance(full_name, str)
        and full_name.casefold() == f"{context.owner}/{context.repo}".casefold()
    )
