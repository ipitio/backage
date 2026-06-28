"""Fetch and parse GitHub owner package listing pages."""

from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Protocol
from urllib.parse import parse_qs, urlencode, urlsplit

from .database_models import OwnerScanPackage

_PAGE_SIZE = 100
_OWNER_TYPES = frozenset({"orgs", "users"})
_PUBLIC_ONLY_MODE_LIMIT = 2
_PRIVATE_CAPABLE_MODE = 3
_PRIVATE_ONLY_MODE = 5
_PACKAGE_PATH_PARTS = 6
_REPOSITORY_PATH_PARTS = 2


class PackageDiscoveryError(RuntimeError):
    """An owner package listing could not be requested safely."""


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
class _Anchor:
    href: str
    relations: frozenset[str]


class _AnchorParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.anchors: list[_Anchor] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        if tag.casefold() != "a":
            return
        values = {key.casefold(): value or "" for key, value in attrs}
        href = values.get("href", "")
        if not href:
            return
        self.anchors.append(
            _Anchor(href, frozenset(values.get("rel", "").casefold().split()))
        )


def parse_package_listing_html(
    html: str,
    request: PackageListingRequest,
) -> PackageListingPage:
    """Parse package identities and pagination from one GitHub HTML page."""

    parser = _AnchorParser()
    parser.feed(html)
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
