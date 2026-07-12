"""Tests for public HTML owner discovery fallback."""

from __future__ import annotations

import httpx

from bkg_py.discovery_fallback import PublicHtmlDiscoveryTraversal
from bkg_py.github import GitHubClient, GitHubSettings


def _client(handler: httpx.MockTransport) -> GitHubClient:
    return GitHubClient(
        GitHubSettings(
            token="-".join(("configured", "token")),
            total_timeout=30,
            initial_backoff=0,
            max_backoff=0,
        ),
        client=httpx.Client(transport=handler),
    )


def test_public_profile_organization_discovery_never_sends_token() -> None:
    """Profile organization links are scraped without authentication."""

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://github.com/Example"
        assert "authorization" not in request.headers
        return httpx.Response(
            200,
            text=(
                '<a href="/orgs/FirstOrg/people">first</a>'
                '<a href="https://github.com/orgs/SecondOrg">second</a>'
                '<a href="https://example.com/orgs/External">external</a>'
            ),
        )

    traversal = PublicHtmlDiscoveryTraversal(_client(httpx.MockTransport(respond)))

    assert traversal.organization_logins("12/Example") == ("FirstOrg", "SecondOrg")


def test_public_organization_membership_uses_paged_people_html() -> None:
    """An organization people page selects member rather than user-org discovery."""

    requested: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        assert "authorization" not in request.headers
        requested.append(str(request.url))
        if request.url.params.get("page") is None:
            return httpx.Response(
                200,
                text='<a href="/orgs/Example/people">People</a>',
            )
        return httpx.Response(
            200,
            text=(
                '<a href="/navigation">navigation</a>'
                '<a data-hovercard-type="user" href="/MemberOne">one</a>'
                '<a data-hovercard-type="organization" href="/MemberOrg">org</a>'
            ),
        )

    traversal = PublicHtmlDiscoveryTraversal(_client(httpx.MockTransport(respond)))

    assert traversal.membership("10/Example") == ("MemberOne", "MemberOrg")
    assert requested == [
        "https://github.com/orgs/Example/people",
        "https://github.com/orgs/Example/people?page=1",
    ]


def test_public_repository_discovery_keeps_api_collaborators_unauthenticated() -> None:
    """HTML edges and the public REST collaborator edge omit the token."""

    requested: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        assert "authorization" not in request.headers
        requested.append(str(request.url))
        if request.url.host == "api.github.com":
            return httpx.Response(200, json=[{"id": 22, "login": "Collaborator"}])
        edge = request.url.path.rsplit("/", maxsplit=1)[-1]
        return httpx.Response(
            200,
            text=(
                f'<a data-hovercard-type="user" href="/{edge}-owner">edge</a>'
                '<a data-hovercard-type="user" href="/Example">self</a>'
            ),
        )

    traversal = PublicHtmlDiscoveryTraversal(_client(httpx.MockTransport(respond)))

    assert traversal.explore("Example/Project") == (
        "stargazers-owner",
        "watchers-owner",
        "forks-owner",
        "22/Collaborator",
    )
    assert requested == [
        "https://github.com/Example/Project/stargazers?page=1",
        "https://github.com/Example/Project/watchers?page=1",
        "https://github.com/Example/Project/forks?page=1",
        "https://api.github.com/repos/Example/Project/collaborators?per_page=100",
    ]
