"""Fetch and parse GitHub owner package listing pages."""

from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Protocol, cast
from urllib.parse import parse_qs, quote, urlencode, urlsplit

from ..database.models import OwnerScanPackage
from ..github import GitHubJsonResponse, GitHubNotFoundError

_PAGE_SIZE = 100
_OWNER_TYPES = frozenset({"orgs", "users"})
_PUBLIC_ONLY_MODE_LIMIT = 2
_PRIVATE_CAPABLE_MODE = 3
_PRIVATE_ONLY_MODE = 5
_PACKAGE_PATH_PARTS = 6
_REPOSITORY_PATH_PARTS = 2
_API_PACKAGE_TYPES = ("container", "npm", "maven", "rubygems", "nuget", "docker")


class PackageDiscoveryError(RuntimeError):
    """An owner package listing could not be requested safely."""


class _UnrecognizedPackageListingError(PackageDiscoveryError):
    """A fetched page does not establish a package listing or its absence."""


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
    Protocol,
):  # pylint: disable=too-few-public-methods
    """GitHub operations used to classify a missing package listing."""

    def rest_json_optional(self, path: str) -> GitHubJsonResponse | None:
        """Return owner metadata or an absent-resource marker."""

        raise NotImplementedError


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

    def url(self) -> str:
        """Build the GitHub HTML listing URL for this request."""

        query: list[tuple[str, str | int]] = []
        if self.owner_type == "users":
            path = f"https://github.com/{self.owner}"
            query.append(("tab", "packages"))
        else:
            path = f"https://github.com/orgs/{self.owner}/packages"
        if self.mode < _PUBLIC_ONLY_MODE_LIMIT:
            query.append(("visibility", "public"))
        elif self.mode == _PRIVATE_ONLY_MODE:
            query.append(("visibility", "private"))
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


@dataclass(frozen=True)
class _Anchor:
    href: str
    relations: frozenset[str]


@dataclass
class _EmptyListingRegion:
    """Empty-state markers within one package results region."""

    depth: int = 1
    blank_depth: int = 0
    heading_parts: list[str] | None = None
    zero_count: bool = False
    empty_heading: bool = False


class _ListingParser(HTMLParser):
    def __init__(self, owner_type: str) -> None:
        super().__init__(convert_charrefs=True)
        self.anchors: list[_Anchor] = []
        self.empty_listing = False
        self._container = (
            "user-packages-list" if owner_type == "users" else "org-packages"
        )
        self._region: _EmptyListingRegion | None = None

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        values = {key.casefold(): value or "" for key, value in attrs}
        self._observe_start(tag, values)
        if tag != "a":
            return
        href = values.get("href", "")
        if not href:
            return
        self.anchors.append(
            _Anchor(href, frozenset(values.get("rel", "").casefold().split()))
        )

    def _observe_start(self, tag: str, values: dict[str, str]) -> None:
        if tag == "div":
            if self._region is not None:
                self._region.depth += 1
            elif values.get("id") == self._container:
                self._region = _EmptyListingRegion()
        region = self._region
        if region is None:
            return
        if tag == "div" and "blankslate" in values.get("class", "").split():
            region.blank_depth = region.depth
        elif tag == "h3":
            region.heading_parts = []

    def handle_data(self, data: str) -> None:
        region = self._region
        if region is not None and region.heading_parts is not None:
            region.heading_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        region = self._region
        if region is None:
            return
        if tag == "h3" and region.heading_parts is not None:
            heading = " ".join("".join(region.heading_parts).split()).casefold()
            region.zero_count = region.zero_count or heading == "0 packages"
            region.empty_heading = region.empty_heading or (
                region.blank_depth > 0 and heading == "no results matched your search."
            )
            region.heading_parts = None
        if tag == "div":
            if region.depth == region.blank_depth:
                region.blank_depth = 0
            region.depth -= 1
            if region.depth == 0:
                self.empty_listing = self.empty_listing or (
                    region.zero_count and region.empty_heading
                )
                self._region = None


def parse_package_listing_html(
    html: str,
    request: PackageListingRequest,
) -> PackageListingPage:
    """Parse package identities and pagination from one GitHub HTML page."""

    parser = _ListingParser(request.owner_type)
    parser.feed(html)
    parser.close()
    packages: dict[tuple[str, str], OwnerScanPackage] = {}
    pending: tuple[str, str] | None = None

    for anchor in parser.anchors:
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
    has_more = (
        any(
            "next" in anchor.relations
            or _links_to_later_page(anchor.href, request.page)
            for anchor in parser.anchors
        )
        or len(unique_packages) >= _PAGE_SIZE
    )
    if not unique_packages and (not parser.empty_listing or has_more):
        raise _UnrecognizedPackageListingError(
            f"unrecognized package listing for {request.owner} page {request.page}; "
            f"html_chars={len(html)} anchors={len(parser.anchors)} "
            f"empty_state={parser.empty_listing} has_more={has_more}"
        )
    return PackageListingPage(unique_packages, has_more)


def _package_path(
    href: str,
    request: PackageListingRequest,
) -> tuple[str, str] | None:
    parts = _github_path_parts(href)
    if len(parts) != _PACKAGE_PATH_PARTS:
        return None
    if parts[:3] != [request.owner_type, request.owner, "packages"]:
        return None
    if parts[4] != "package" or not parts[3] or not parts[5]:
        return None
    return parts[3], parts[5]


def _repository_path(href: str, owner: str) -> str | None:
    parts = _github_path_parts(href)
    if len(parts) != _REPOSITORY_PATH_PARTS or parts[0] != owner or not parts[1]:
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
        return parse_package_listing_html(html, request)


def fetch_package_listing_page(
    client: OwnerListingClient,
    request: PackageListingRequest,
    *,
    verify_empty_with_api: bool = False,
) -> PackageListingFetch:
    """Fetch a listing, separating verified absence from unavailable inventory."""

    try:
        page = PackageListingService(client).fetch(request)
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
        empty, api_diagnostic = _verify_empty_owner_listing(client, request)
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


def _verify_empty_owner_listing(
    client: OwnerListingClient, request: PackageListingRequest
) -> tuple[bool, str]:
    owner_path = f"{request.owner_type}/{quote(request.owner, safe='')}/packages"
    for package_type in _API_PACKAGE_TYPES:
        query: list[tuple[str, str | int]] = [
            ("package_type", package_type),
            ("per_page", 1),
            ("page", 1),
        ]
        if not request.authenticated:
            query.append(("visibility", "public"))
        elif request.mode == _PRIVATE_ONLY_MODE:
            query.append(("visibility", "private"))
        response = client.rest_json_optional(f"{owner_path}?{urlencode(query)}")
        if response is None:
            return False, f"Package API unavailable for {package_type}"
        value: object = response.value
        if not isinstance(value, list) or response.next_url is not None:
            return (
                False,
                f"Package API did not establish empty inventory for {package_type}",
            )
        if cast(list[object], value):
            return False, f"Package API returned nonempty inventory for {package_type}"
    return True, f"Verified empty package listing for {request.owner} via package API"
