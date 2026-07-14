"""Public GitHub HTML fallback for owner discovery."""

from __future__ import annotations

from collections.abc import Callable
from html.parser import HTMLParser
from typing import cast
from urllib.parse import quote, urlsplit

from .discovery import DiscoveryError, owner_ref_login
from .github import GitHubClient, GitHubError, GitHubNotFoundError
from .owners import normalize_owner_lines

_GITHUB_WEB_ROOT = "https://github.com"
_HTML_PAGE_SIZE = 15
_ORGANIZATION_PATH_SEGMENTS = 2
_OWNER_EDGES = ("followers", "following", "people")
_REPOSITORY_EDGES = ("stargazers", "watchers", "forks", "collaborators")
_OWNER_HOVERCARD_TYPES = frozenset({"organization", "user"})
_PUBLIC_NAV_PATHS = frozenset(
    {
        "about",
        "apps",
        "collections",
        "customer-stories",
        "enterprise",
        "events",
        "explore",
        "features",
        "issues",
        "login",
        "marketplace",
        "new",
        "notifications",
        "organizations",
        "orgs",
        "pricing",
        "pulls",
        "search",
        "security",
        "settings",
        "signup",
        "site",
        "solutions",
        "sponsors",
        "topics",
        "trending",
        "users",
    }
)


class _AnchorParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.anchors: list[tuple[str, str]] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        if tag.casefold() != "a":
            return
        values = {key.casefold(): value or "" for key, value in attrs}
        href = values.get("href", "")
        if href:
            self.anchors.append((href, values.get("data-hovercard-type", "")))


class PublicHtmlDiscoveryTraversal:  # pylint: disable=too-few-public-methods
    """Best-effort public traversal used when authenticated discovery is unavailable."""

    def __init__(
        self,
        client: GitHubClient,
        *,
        diagnostic: Callable[[str], None] = lambda _message: None,
    ) -> None:
        self.client = client
        self.diagnostic = diagnostic

    def explore(self, node: str, edge: str = "") -> tuple[str, ...]:
        """Return public connections for one owner or repository."""

        if "/" in node:
            return self._explore_repository(node, edge)
        return self._explore_owner(node, edge)

    def organization_logins(self, owner_ref: str) -> tuple[str, ...]:
        """Return public organizations linked from one owner profile."""

        owner = owner_ref_login(owner_ref)
        if not owner:
            return ()
        html = self._get_html(f"/{quote(owner, safe='')}")
        if html is None:
            return ()
        return normalize_owner_lines(_organization_logins(html))

    def membership(self, owner_ref: str) -> tuple[str, ...]:
        """Return public organization members or user organizations."""

        owner = owner_ref_login(owner_ref)
        if not owner:
            return ()
        people_path = f"/orgs/{quote(owner, safe='')}/people"
        people_html = self._get_html(people_path)
        if people_html is not None and _contains_path(people_html, people_path):
            return self._paged_owner_edge(owner, "people", organization=True)
        return self.organization_logins(owner)

    def _explore_repository(self, node: str, edge: str) -> tuple[str, ...]:
        owner, separator, repository = node.partition("/")
        if not separator or not owner or not repository:
            raise DiscoveryError(f"invalid repository discovery target: {node}")
        edges = (edge,) if edge else _REPOSITORY_EDGES
        values: list[str] = []
        for current_edge in edges:
            if current_edge == "collaborators":
                values.extend(self._public_collaborators(owner, repository))
            elif current_edge in _REPOSITORY_EDGES:
                values.extend(
                    self._paged_html_logins(
                        lambda page, current_edge=current_edge: (
                            f"/{quote(owner, safe='')}/{quote(repository, safe='')}"
                            f"/{current_edge}?page={page}"
                        ),
                        owner,
                    )
                )
            else:
                raise DiscoveryError(
                    f"unsupported repository discovery edge: {current_edge}"
                )
        return normalize_owner_lines(values)

    def _explore_owner(self, owner: str, edge: str) -> tuple[str, ...]:
        if not owner:
            return ()
        edges = (edge,) if edge else _OWNER_EDGES
        if any(current_edge not in _OWNER_EDGES for current_edge in edges):
            raise DiscoveryError(f"unsupported owner discovery edge: {edge}")

        organization = self._is_organization(owner)
        values: list[str] = []
        for current_edge in edges:
            values.extend(
                self._paged_owner_edge(
                    owner,
                    current_edge,
                    organization=organization,
                )
            )
        if not organization:
            values.extend(self.organization_logins(owner))
        return normalize_owner_lines(values)

    def _paged_owner_edge(
        self,
        owner: str,
        edge: str,
        *,
        organization: bool,
    ) -> tuple[str, ...]:
        if organization:
            return self._paged_html_logins(
                lambda page: f"/orgs/{quote(owner, safe='')}/{edge}?page={page}",
                owner,
            )
        return self._paged_html_logins(
            lambda page: f"/{quote(owner, safe='')}?tab={edge}&page={page}",
            owner,
        )

    def _paged_html_logins(
        self,
        path_for_page: Callable[[int], str],
        owner: str,
    ) -> tuple[str, ...]:
        page = 1
        values: list[str] = []
        previous_page: tuple[str, ...] | None = None
        while True:
            html = self._get_html(path_for_page(page))
            if html is None:
                break
            page_values = _owner_logins(html)
            if page_values == previous_page:
                break
            previous_page = page_values
            values.extend(
                value for value in page_values if value.casefold() != owner.casefold()
            )
            if len(page_values) < _HTML_PAGE_SIZE:
                break
            page += 1
        return normalize_owner_lines(values)

    def _public_collaborators(
        self,
        owner: str,
        repository: str,
    ) -> tuple[str, ...]:
        path = (
            f"repos/{quote(owner, safe='')}/{quote(repository, safe='')}"
            "/collaborators?per_page=100"
        )
        values: list[str] = []
        try:
            responses = self.client.rest_pages(path, authenticated=False)
            for response in responses:
                response_value: object = response.value
                if not isinstance(response_value, list):
                    raise DiscoveryError(
                        "invalid public collaborators response for "
                        f"{owner}/{repository}"
                    )
                for item in cast(list[object], response_value):
                    if not isinstance(item, dict):
                        continue
                    node = cast(dict[str, object], item)
                    login = node.get("login")
                    owner_id = node.get("id")
                    if (
                        isinstance(login, str)
                        and login
                        and isinstance(owner_id, int)
                        and owner_id > 0
                        and login.casefold() != owner.casefold()
                    ):
                        values.append(f"{owner_id}/{login}")
        except GitHubError as error:
            self.diagnostic(
                f"Public collaborator discovery unavailable for {owner}/{repository}: "
                f"{error}"
            )
        return normalize_owner_lines(values)

    def _is_organization(self, owner: str) -> bool:
        people_path = f"/orgs/{quote(owner, safe='')}/people"
        html = self._get_html(people_path)
        return html is not None and _contains_path(html, people_path)

    def _get_html(self, path: str) -> str | None:
        try:
            return self.client.get_text(
                f"{_GITHUB_WEB_ROOT}{path}",
                authenticated=False,
            )
        except GitHubNotFoundError:
            return None


def _anchors(html: str) -> tuple[tuple[str, str], ...]:
    parser = _AnchorParser()
    parser.feed(html)
    parser.close()
    return tuple(parser.anchors)


def _github_path(href: str) -> str | None:
    parsed = urlsplit(href)
    if parsed.netloc and parsed.netloc.casefold() not in {
        "github.com",
        "www.github.com",
    }:
        return None
    return parsed.path if parsed.path.startswith("/") else None


def _owner_logins(html: str) -> tuple[str, ...]:
    anchors = _anchors(html)
    preferred = tuple(
        anchor for anchor in anchors if anchor[1].casefold() in _OWNER_HOVERCARD_TYPES
    )
    candidates = preferred or anchors
    values: list[str] = []
    for href, _hovercard_type in candidates:
        path = _github_path(href)
        if path is None:
            continue
        segments = tuple(segment for segment in path.split("/") if segment)
        if len(segments) != 1:
            continue
        login = segments[0]
        if login.casefold() not in _PUBLIC_NAV_PATHS:
            values.append(login)
    return normalize_owner_lines(values)


def _organization_logins(html: str) -> tuple[str, ...]:
    values: list[str] = []
    for href, _hovercard_type in _anchors(html):
        path = _github_path(href)
        if path is None:
            continue
        segments = tuple(segment for segment in path.split("/") if segment)
        if len(segments) >= _ORGANIZATION_PATH_SEGMENTS and segments[0] == "orgs":
            values.append(segments[1])
    return normalize_owner_lines(values)


def _contains_path(html: str, expected: str) -> bool:
    return any(_github_path(href) == expected for href, _kind in _anchors(html))
