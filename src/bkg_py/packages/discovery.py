"""Fetch and parse GitHub owner package listing pages."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import parse_qs, quote, urlencode, urlsplit

from ..database.models import OwnerScanPackage
from ..github import GitHubError, GitHubNotFoundError
from .inventory_probe import PackageInventoryClient, PackageInventoryProbe
from .legacy import (
    LegacyPackageLink,
    legacy_package_link,
    parse_legacy_package_identity,
)
from .listing_html import ListingHTMLParser, ListingMarkup

_PAGE_SIZE = 100
_OWNER_TYPES = frozenset({"orgs", "users"})
_PUBLIC_ONLY_MODE_LIMIT = 2
_PRIVATE_CAPABLE_MODE = 3
_PRIVATE_ONLY_MODE = 5
_PACKAGE_PATH_PARTS = 6
_REPOSITORY_PATH_PARTS = 2
_DIAGNOSTIC_PATH_LIMIT = 3
_DIAGNOSTIC_PATH_LENGTH = 160


class PackageDiscoveryError(RuntimeError):
    """An owner package listing could not be requested safely."""


class _UnrecognizedPackageListingError(PackageDiscoveryError):
    """A fetched page does not establish a package listing or its absence."""


class _IncompletePackageListingError(_UnrecognizedPackageListingError):
    """Visible entries or a partial results region prevent complete coverage."""


class PackageListingClient(Protocol):  # pylint: disable=too-few-public-methods
    """HTTP operation required by owner package listing discovery."""

    def get_text(
        self,
        url: str,
        *,
        authenticated: bool = False,
        accept: str = "text/html",
    ) -> str:
        """Request one text response."""

        raise NotImplementedError


class OwnerListingClient(
    PackageListingClient,
    PackageInventoryClient,
    Protocol,
):  # pylint: disable=too-few-public-methods
    """GitHub operations used to classify a missing package listing."""


@dataclass(frozen=True)
class PackageListingRequest:
    """Inputs for one owner package listing page."""

    owner_type: str
    owner: str
    page: int
    mode: int

    def __post_init__(self) -> None:
        if self.owner_type not in _OWNER_TYPES:
            raise PackageDiscoveryError(f"unsupported owner type: {self.owner_type}")
        if not self.owner:
            raise PackageDiscoveryError("owner is required")
        if self.page < 1:
            raise PackageDiscoveryError("package listing page must be positive")

    @property
    def authenticated(self) -> bool:
        """Return whether the listing may include private packages."""

        return self.mode >= _PRIVATE_CAPABLE_MODE

    @property
    def visibility(self) -> str | None:
        """Return the explicit visibility filter selected by the listing mode."""

        if self.mode < _PUBLIC_ONLY_MODE_LIMIT:
            return "public"
        if self.mode == _PRIVATE_ONLY_MODE:
            return "private"
        return None

    def url(self) -> str:
        """Build the GitHub HTML listing URL for this request."""

        query: list[tuple[str, str | int]] = []
        if self.owner_type == "users":
            path = f"https://github.com/{self.owner}"
            query.append(("tab", "packages"))
        else:
            path = f"https://github.com/orgs/{self.owner}/packages"
        if self.visibility is not None:
            query.append(("visibility", self.visibility))
        query.extend((("per_page", _PAGE_SIZE), ("page", self.page)))
        return f"{path}?{urlencode(query)}"


@dataclass(frozen=True)
class PackageListingPage:
    """One parsed owner package listing page."""

    packages: tuple[OwnerScanPackage, ...]
    has_more: bool


@dataclass(frozen=True)
class PackageListingFetch:
    """One listing page classified against the owner's current existence."""

    page: PackageListingPage
    owner_missing: bool = False
    listing_unavailable: bool = False
    diagnostic: str = ""


def parse_package_listing_html(
    html: str,
    request: PackageListingRequest,
    *,
    resolve_legacy: Callable[[LegacyPackageLink], OwnerScanPackage | None]
    | None = None,
) -> PackageListingPage:
    """Parse package identities and pagination from one GitHub HTML page."""

    parser = ListingHTMLParser(request.owner_type)
    parser.feed(html)
    parser.close()
    packages = _typed_packages(parser.markup, request)
    legacy_links = {
        urlsplit(anchor.href).path: link
        for anchor in parser.markup.entry_anchors
        if (link := legacy_package_link(anchor.href, request.owner)) is not None
    }
    resolved_paths: set[str] = set()
    if resolve_legacy is not None and legacy_links:
        # Reject truncated or incomplete listings before requesting detail pages.
        _require_listing_coverage(
            parser.markup,
            request,
            len(packages) + len(legacy_links),
            resolved_paths=set(legacy_links),
        )
        for path, link in legacy_links.items():
            identity = resolve_legacy(link)
            if identity is not None:
                key = (identity.package_type, identity.package)
                previous = packages.get(key)
                if previous is not None and (
                    previous != identity
                    or previous.source_package_id != identity.source_package_id
                ):
                    continue
                packages[key] = identity
                resolved_paths.add(path)

    unique_packages = tuple(
        sorted(
            packages.values(),
            key=lambda package: (
                package.package_type,
                package.repo,
                package.package,
            ),
        )
    )
    _require_listing_coverage(
        parser.markup, request, len(unique_packages), resolved_paths=resolved_paths
    )
    has_more = (
        any(
            _is_listing_pagination(anchor.href, request)
            and (
                "next" in anchor.relations
                or _links_to_later_page(anchor.href, request.page)
            )
            for anchor in parser.markup.anchors
        )
        or max(len(unique_packages), parser.markup.entry_count) >= _PAGE_SIZE
    )
    if not unique_packages and (not parser.markup.empty_listing or has_more):
        raise _UnrecognizedPackageListingError(
            f"unrecognized package listing for {request.owner} page {request.page}; "
            f"html_chars={len(html)} anchors={len(parser.markup.anchors)} "
            f"empty_state={parser.markup.empty_listing} has_more={has_more}"
        )
    return PackageListingPage(unique_packages, has_more)


def _typed_packages(
    markup: ListingMarkup, request: PackageListingRequest
) -> dict[tuple[str, str], OwnerScanPackage]:
    """Associate typed package links with repositories without crossing legacy rows."""

    packages: dict[tuple[str, str], OwnerScanPackage] = {}
    pending: tuple[str, str] | None = None

    for anchor in markup.entry_anchors:
        if legacy_package_link(anchor.href, request.owner) is not None:
            if pending is not None:
                packages.setdefault(
                    pending, _package_without_repository(request, pending)
                )
                pending = None
            continue
        package = _package_path(anchor.href, request)
        if package is not None:
            if package == pending:
                continue
            if pending is not None:
                packages.setdefault(
                    pending,
                    _package_without_repository(request, pending),
                )
            pending = package
            continue

        repository = _repository_path(anchor.href, request.owner)
        if pending is not None and repository is not None:
            package_type, package_name = pending
            packages[pending] = OwnerScanPackage(
                request.owner_type,
                package_type,
                repository,
                package_name,
            )
            pending = None

    if pending is not None:
        packages.setdefault(
            pending,
            _package_without_repository(request, pending),
        )

    return packages


def _require_listing_coverage(
    markup: ListingMarkup,
    request: PackageListingRequest,
    recognized_count: int,
    *,
    resolved_paths: set[str] | None = None,
) -> None:
    unresolved = {
        urlsplit(anchor.href).path
        for anchor in markup.entry_anchors
        if _is_unresolved_package_link(anchor.href, request)
        and urlsplit(anchor.href).path not in (resolved_paths or ())
    }
    coverage_count = (
        _recognized_rows(markup, request, resolved_paths or set())
        if markup.entry_rows
        else recognized_count
    )
    issues: list[str] = []
    if unresolved:
        samples = ",".join(
            path[:_DIAGNOSTIC_PATH_LENGTH]
            for path in sorted(unresolved)[:_DIAGNOSTIC_PATH_LIMIT]
        )
        issues.append(
            f"unsupported package links (including repository-scoped entries): "
            f"count={len(unresolved)} paths={samples}"
        )
    if markup.has_region and not markup.complete_region:
        issues.append("package results region is incomplete")
    if markup.package_count is not None and markup.package_count != coverage_count:
        issues.append(
            f"package count mismatch: declared={markup.package_count} "
            f"recognized={coverage_count}"
        )
    if markup.entry_count and markup.entry_count != coverage_count:
        issues.append(
            f"package row coverage mismatch: rows={markup.entry_count} "
            f"recognized={coverage_count}"
        )
    if issues:
        raise _IncompletePackageListingError(
            f"unrecognized package listing for {request.owner} page {request.page}; "
            + "; ".join(issues)
        )


def _recognized_rows(
    markup: ListingMarkup, request: PackageListingRequest, resolved_paths: set[str]
) -> int:
    """Require one recognized route per row, without counting repeated anchors."""

    recognized = 0
    for row in markup.entry_rows:
        routes: set[tuple[str, ...]] = set()
        for anchor in row:
            package = _package_path(anchor.href, request)
            if package is not None:
                routes.add(("typed", *package))
            path = urlsplit(anchor.href).path
            if path in resolved_paths:
                routes.add(("legacy", path))
        recognized += len(routes) == 1
    return recognized


def _is_unresolved_package_link(href: str, request: PackageListingRequest) -> bool:
    if _package_path(href, request) is not None:
        return False
    parts = _github_path_parts(href)
    if len(parts) <= _REPOSITORY_PATH_PARTS + 1:
        return False
    return (
        parts[0] == request.owner_type
        and parts[1].casefold() == request.owner.casefold()
        and parts[2] == "packages"
    ) or (
        parts[0].casefold() == request.owner.casefold()
        and parts[2] in {"packages", "pkgs"}
    )


def _package_path(
    href: str,
    request: PackageListingRequest,
) -> tuple[str, str] | None:
    parts = _github_path_parts(href)
    if len(parts) != _PACKAGE_PATH_PARTS:
        return None
    if (
        parts[0] != request.owner_type
        or parts[1].casefold() != request.owner.casefold()
        or parts[2] != "packages"
    ):
        return None
    if parts[4] != "package" or not parts[3] or not parts[5]:
        return None
    return parts[3], parts[5]


def _repository_path(href: str, owner: str) -> str | None:
    parts = _github_path_parts(href)
    if (
        len(parts) != _REPOSITORY_PATH_PARTS
        or parts[0].casefold() != owner.casefold()
        or not parts[1]
    ):
        return None
    return parts[1]


def _github_path_parts(href: str) -> list[str]:
    parsed = urlsplit(href)
    if parsed.netloc and parsed.netloc.casefold() not in {
        "github.com",
        "www.github.com",
    }:
        return []
    return parsed.path.strip("/").split("/")


def _package_without_repository(
    request: PackageListingRequest,
    pending: tuple[str, str],
) -> OwnerScanPackage:
    package_type, package_name = pending
    return OwnerScanPackage(
        request.owner_type,
        package_type,
        package_name,
        package_name,
    )


def _links_to_later_page(href: str, current_page: int) -> bool:
    for value in parse_qs(urlsplit(href).query).get("page", ()):
        try:
            if int(value) > current_page:
                return True
        except ValueError:
            continue
    return False


def _is_listing_pagination(href: str, request: PackageListingRequest) -> bool:
    parsed = urlsplit(href)
    if parsed.netloc and not _github_path_parts(href):
        return False
    path = parsed.path.rstrip("/").casefold()
    expected = urlsplit(request.url()).path.casefold()
    return (not path or path == expected) and parse_qs(parsed.query).get(
        "tab", ["packages"]
    ) == ["packages"]


class PackageListingService:  # pylint: disable=too-few-public-methods
    """Load owner package listings through a shared GitHub client."""

    def __init__(self, client: PackageListingClient) -> None:
        self.client = client

    def fetch(self, request: PackageListingRequest) -> PackageListingPage:
        """Fetch and parse one package listing page."""

        html = self.client.get_text(
            request.url(),
            authenticated=request.authenticated,
        )

        def resolve(link: LegacyPackageLink) -> OwnerScanPackage | None:
            try:
                detail = self.client.get_text(
                    link.url, authenticated=request.authenticated
                )
            except GitHubError:
                return None
            return parse_legacy_package_identity(detail, link, request.owner_type)

        return parse_package_listing_html(html, request, resolve_legacy=resolve)


def fetch_package_listing_page(
    client: OwnerListingClient,
    request: PackageListingRequest,
    *,
    verify_empty_with_api: bool = False,
    inventory_probe: PackageInventoryProbe | None = None,
) -> PackageListingFetch:
    """Fetch a listing, separating verified absence from unavailable inventory."""

    try:
        page = PackageListingService(client).fetch(request)
    except _IncompletePackageListingError as error:
        return PackageListingFetch(
            PackageListingPage((), False),
            listing_unavailable=True,
            diagnostic=str(error),
        )
    except _UnrecognizedPackageListingError as error:
        diagnostic = str(error)
    except GitHubNotFoundError:
        owner_path = f"{request.owner_type}/{quote(request.owner, safe='')}"
        if client.rest_json_optional(owner_path) is None:
            return PackageListingFetch(PackageListingPage((), False), True)
        diagnostic = (
            f"Package listing returned HTTP 404 for existing owner "
            f"{request.owner} page {request.page}"
        )
    else:
        if page.packages or not request.authenticated or request.page > 1:
            return PackageListingFetch(page)
        diagnostic = (
            f"Empty private-capable package listing for "
            f"{request.owner} page {request.page} requires API verification"
        )

    if request.page == 1 and verify_empty_with_api:
        probe = (
            inventory_probe if inventory_probe is not None else PackageInventoryProbe()
        )
        empty, api_diagnostic = probe.verify_empty(
            client,
            request.owner_type,
            request.owner,
            visibility=request.visibility if request.authenticated else "public",
        )
        if empty:
            return PackageListingFetch(
                PackageListingPage((), False), diagnostic=api_diagnostic
            )
        diagnostic = f"{diagnostic}; {api_diagnostic}"
    return PackageListingFetch(
        PackageListingPage((), False),
        listing_unavailable=True,
        diagnostic=diagnostic,
    )
