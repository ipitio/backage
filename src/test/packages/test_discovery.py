"""Tests for owner package listing discovery."""

from __future__ import annotations

import pytest

from bkg_py.database.models import OwnerScanPackage
from bkg_py.github import GitHubNotFoundError
from bkg_py.packages.discovery import (
    PackageListingPage,
    PackageListingRequest,
    PackageListingService,
    fetch_package_listing_page,
    parse_package_listing_html,
)

from ..github.fake import FakeGitHubClient


def test_listing_parser_associates_repositories_and_deduplicates_packages() -> None:
    """Packages use their following repository without crossing card boundaries."""

    request = PackageListingRequest("orgs", "Example", 1, 0)
    html = """
        <a href="/orgs/Example/packages/container/package/alpha">alpha</a>
        <a href="/orgs/Example/packages/container/package/alpha">alpha icon</a>
        <a href="/Example/AlphaRepo">repository</a>
        <a href="/orgs/Example/packages/container/package/alpha">alpha</a>
        <a href="/Example/AlphaRepo">repository</a>
        <a href="/orgs/Example/packages/npm/package/tools%2Fworker">worker</a>
        <a href="/orgs/Example/packages/npm/package/beta">beta</a>
        <a href="/Example/BetaRepo">repository</a>
    """

    page = parse_package_listing_html(html, request)

    assert page.packages == (
        OwnerScanPackage("orgs", "container", "AlphaRepo", "alpha"),
        OwnerScanPackage("orgs", "npm", "BetaRepo", "beta"),
        OwnerScanPackage("orgs", "npm", "tools%2Fworker", "tools%2Fworker"),
    )
    assert not page.has_more


def test_listing_parser_uses_pagination_links() -> None:
    """An explicit GitHub next link continues package pagination."""

    request = PackageListingRequest("users", "example", 4, 0)
    html = """
        <a href="/users/example/packages/container/package/demo">demo</a>
        <a href="/example/repository">repository</a>
        <a rel="next" href="?tab=packages&amp;page=5">Next</a>
    """

    assert parse_package_listing_html(html, request).has_more


def test_listing_parser_continues_after_a_full_page_without_metadata() -> None:
    """A full page remains a conservative pagination fallback."""

    request = PackageListingRequest("orgs", "Example", 1, 0)
    html = "".join(
        f'<a href="/orgs/Example/packages/container/package/package-{index}">x</a>'
        for index in range(100)
    )

    page = parse_package_listing_html(html, request)

    assert len(page.packages) == 100
    assert page.has_more


@pytest.mark.parametrize(
    ("listing_request", "expected_url", "authenticated"),
    [
        (
            PackageListingRequest("users", "example", 2, 0),
            "https://github.com/example?"
            "tab=packages&visibility=public&per_page=100&page=2",
            False,
        ),
        (
            PackageListingRequest("orgs", "Example", 3, 4),
            "https://github.com/orgs/Example/packages?per_page=100&page=3",
            True,
        ),
        (
            PackageListingRequest("orgs", "Example", 1, 5),
            "https://github.com/orgs/Example/packages?"
            "visibility=private&per_page=100&page=1",
            True,
        ),
    ],
)
def test_listing_service_preserves_mode_specific_urls(
    listing_request: PackageListingRequest,
    expected_url: str,
    authenticated: bool,
) -> None:
    """The service preserves public, mixed, and private mode behavior."""

    client = FakeGitHubClient(text_values={expected_url: "<div></div>"})

    page = PackageListingService(client).fetch(listing_request)

    assert page == PackageListingPage((), False)
    assert client.text_requests == [expected_url]
    assert client.text_authentication == [authenticated]


def test_listing_404_confirms_missing_owner_before_returning_an_empty_page() -> None:
    """A listing 404 is empty only when the owner API also reports absence."""

    request = PackageListingRequest("users", "departed", 1, 0)
    url = request.url()
    client = FakeGitHubClient(
        rest_values={"users/departed": None},
        text_values={url: GitHubNotFoundError("listing not found")},
    )

    fetched = fetch_package_listing_page(client, request)

    assert fetched.page == PackageListingPage((), False)
    assert fetched.owner_missing
    assert not fetched.listing_unavailable
    assert client.rest_requests == ["users/departed"]


def test_listing_404_verifies_known_packages_when_the_owner_still_exists() -> None:
    """An existing owner with no listing enters package API verification."""

    request = PackageListingRequest("orgs", "available", 1, 0)
    client = FakeGitHubClient(
        rest_values={"orgs/available": {"login": "available"}},
        text_values={request.url(): GitHubNotFoundError("listing not found")},
    )

    fetched = fetch_package_listing_page(client, request)

    assert fetched.page == PackageListingPage((), False)
    assert not fetched.owner_missing
    assert fetched.listing_unavailable
    assert client.rest_requests == ["orgs/available"]
