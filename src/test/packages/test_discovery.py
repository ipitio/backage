"""Tests for owner package listing discovery."""

import pytest

from bkg_py.database.models import OwnerScanPackage
from bkg_py.github import GitHubNotFoundError, GitHubResponseError
from bkg_py.packages.discovery import (
    PackageDiscoveryError,
    PackageListingPage,
    PackageListingRequest,
    PackageListingService,
    fetch_package_listing_page,
    parse_package_listing_html,
)

from ..github.fake import FakeGitHubClient


def _empty_listing(container: str) -> str:
    return f"""
        <div id="{container}">
          <div class="Box-header"><h3><span>0</span> packages</h3></div>
          <div class="blankslate"><svg><path></path></svg>
            <h3>No results matched your search.</h3>
          </div>
        </div>
    """


def _empty_probe_paths(request: PackageListingRequest) -> tuple[str, ...]:
    visibility = ""
    if not request.authenticated:
        visibility = "&visibility=public"
    elif request.mode == 5:
        visibility = "&visibility=private"
    return tuple(
        f"{request.owner_type}/{request.owner}/packages?package_type={kind}"
        f"&per_page=1&page=1{visibility}"
        for kind in ("container", "npm", "maven", "rubygems", "nuget", "docker")
    )


def _listing_region(entries: str, count: int = 1) -> str:
    return (
        '<div id="org-packages"><div class="Box-header">'
        f"<h3>{count} package{'s' if count != 1 else ''}</h3></div>"
        f"<ul>{entries}</ul></div>"
    )


_TYPED_ENTRY = (
    '<li><a href="/orgs/example/packages/container/package/image">image</a>'
    '<a href="/example/repo">repository</a></li>'
)
_LEGACY_ENTRY = (
    '<li><a title="org.example.library" href="/example/repo/packages/12345">'
    'org.example.library</a><a href="/example/repo">repository</a></li>'
)


@pytest.mark.parametrize(
    "html",
    [
        _listing_region(_TYPED_ENTRY + _LEGACY_ENTRY, 2),
        _listing_region(_TYPED_ENTRY, 2),
        _listing_region(_TYPED_ENTRY).rsplit("</div>", maxsplit=1)[0],
        _listing_region(_TYPED_ENTRY, 0),
        '<div id="org-packages"><ul>'
        + _TYPED_ENTRY.replace("<li>", '<li class="Box-row">')
        + '<li class="Box-row"><a href="/example/other-route">unknown</a></li>'
        "</ul></div>",
    ],
    ids=[
        "mixed-links",
        "missing-entry",
        "truncated-region",
        "contradictory-count",
        "unrecognized-row-without-heading",
    ],
)
def test_partial_nonempty_listing_cannot_complete_an_inventory(html: str) -> None:
    """Recognizing some packages does not establish complete page coverage."""

    with pytest.raises(PackageDiscoveryError, match="unrecognized package listing"):
        parse_package_listing_html(html, PackageListingRequest("orgs", "example", 1, 0))


def test_repository_scoped_entries_are_not_overruled_by_an_empty_api() -> None:
    """An API's readable inventory cannot discard packages present in HTML."""

    request = PackageListingRequest("orgs", "example", 1, 0)
    client = FakeGitHubClient(
        rest_values={path: [] for path in _empty_probe_paths(request)},
        text_values={
            request.url(): _listing_region(_LEGACY_ENTRY),
            "https://github.com/example/repo/packages/12345": "<h1>Unknown format</h1>",
        },
    )

    fetched = fetch_package_listing_page(client, request, verify_empty_with_api=True)

    assert fetched.listing_unavailable
    assert "repository-scoped" in fetched.diagnostic
    assert "/example/repo/packages/12345" in fetched.diagnostic
    assert not client.rest_requests


def test_mixed_listing_resolves_maven_coordinates_without_rest() -> None:
    """Repository-scoped Maven packages share a complete page with containers."""

    request = PackageListingRequest("orgs", "example", 1, 0)
    detail_url = "https://github.com/example/repo/packages/12345"
    detail = (
        '<clipboard-copy value="&lt;dependency&gt;'
        "&lt;groupId&gt;org.example&lt;/groupId&gt;"
        "&lt;artifactId&gt;library&lt;/artifactId&gt;"
        '&lt;version&gt;1.2.3&lt;/version&gt;&lt;/dependency&gt;"></clipboard-copy>'
    )
    client = FakeGitHubClient(
        text_values={
            request.url(): _listing_region(_TYPED_ENTRY + _LEGACY_ENTRY, 2),
            detail_url: detail,
        }
    )

    fetched = fetch_package_listing_page(client, request, verify_empty_with_api=True)

    assert not fetched.listing_unavailable
    assert fetched.page.packages == (
        OwnerScanPackage("orgs", "container", "repo", "image"),
        OwnerScanPackage("orgs", "maven", "repo", "org.example.library"),
    )
    assert fetched.page.packages[1].source_package_id == "12345"
    assert client.text_requests == [request.url(), detail_url]
    assert not client.rest_requests


def test_partial_legacy_listing_is_rejected_before_detail_requests() -> None:
    """A truncated inventory cannot spend requests on resolving its entries."""

    request = PackageListingRequest("orgs", "example", 1, 0)
    client = FakeGitHubClient(
        text_values={
            request.url(): _listing_region(_LEGACY_ENTRY).rsplit("</div>", maxsplit=1)[
                0
            ]
        }
    )

    fetched = fetch_package_listing_page(client, request)

    assert fetched.listing_unavailable
    assert client.text_requests == [request.url()]


def test_duplicate_rows_preserve_coverage_without_duplicate_package_work() -> None:
    """GitHub can advertise the same route twice without an unknown entry."""

    entry = _TYPED_ENTRY.replace("<li>", '<li class="Box-row">')
    page = parse_package_listing_html(
        _listing_region(entry + entry, 2),
        PackageListingRequest("orgs", "example", 1, 0),
    )
    assert page.packages == (OwnerScanPackage("orgs", "container", "repo", "image"),)


def test_duplicate_anchors_cannot_cover_an_unknown_row() -> None:
    """Two links to one package within a row do not validate another row."""

    entry = _TYPED_ENTRY.replace("<li>", '<li class="Box-row">').replace(
        "</li>",
        '<a href="/orgs/example/packages/container/package/image">icon</a></li>',
    )
    html = _listing_region(entry + '<li class="Box-row">unknown</li>', 2)
    with pytest.raises(PackageDiscoveryError, match="row coverage mismatch"):
        parse_package_listing_html(html, PackageListingRequest("orgs", "example", 1, 0))


def test_legacy_namesakes_cannot_collapse_distinct_numeric_packages() -> None:
    """Separate legacy package IDs with the same coordinates stay unresolved."""

    request = PackageListingRequest("orgs", "example", 1, 0)
    entries = _LEGACY_ENTRY.replace("<li>", '<li class="Box-row">')
    html = _listing_region(entries + entries.replace("12345", "54321"), 2)

    with pytest.raises(PackageDiscoveryError, match="unsupported package links"):
        parse_package_listing_html(
            html,
            request,
            resolve_legacy=lambda link: OwnerScanPackage(
                "orgs", "maven", link.repo, "org.example.library", link.package_id
            ),
        )


def test_listing_coverage_ignores_links_outside_the_results_region() -> None:
    """Global navigation and unrelated packages do not alter the owner's page."""

    html = (
        '<a href="/orgs/example/packages/npm/package/navigation">outside</a>'
        '<a rel="next" href="https://other.example/?page=2">outside next</a>'
        + _listing_region(_TYPED_ENTRY)
    )

    page = parse_package_listing_html(
        html, PackageListingRequest("orgs", "EXAMPLE", 1, 0)
    )

    assert page == PackageListingPage(
        (OwnerScanPackage("orgs", "container", "repo", "image"),), False
    )


def test_api_failure_retains_listing_context() -> None:
    """An unavailable emptiness probe retains both owner and HTTP diagnostics."""

    request = PackageListingRequest("users", "example", 1, 0)
    client = FakeGitHubClient(
        rest_values={
            _empty_probe_paths(request)[0]: GitHubResponseError(
                "GitHub returned HTTP 400: Invalid argument.", status_code=400
            )
        },
        text_values={request.url(): "<div>changed upstream markup</div>"},
    )

    fetched = fetch_package_listing_page(client, request, verify_empty_with_api=True)

    assert fetched.listing_unavailable
    assert "example page 1" in fetched.diagnostic
    assert "HTTP 400" in fetched.diagnostic


@pytest.mark.parametrize(
    ("owner_type", "container"),
    [("users", "user-packages-list"), ("orgs", "org-packages")],
)
def test_listing_parser_recognizes_explicit_empty_results(
    owner_type: str, container: str
) -> None:
    """The complete package results region explicitly reports zero matches."""

    request = PackageListingRequest(owner_type, "example", 1, 0)
    assert parse_package_listing_html(_empty_listing(container), request) == (
        PackageListingPage((), False)
    )


@pytest.mark.parametrize(
    "html",
    [
        "<div></div>",
        "<h1>Sign in to GitHub</h1>",
        _empty_listing("unrelated-results"),
        _empty_listing("org-packages").replace("0</span>", "1</span>"),
        _empty_listing("org-packages").replace("blankslate", "unrelated"),
        _empty_listing("org-packages").rsplit("</div>", maxsplit=1)[0],
        _empty_listing("org-packages") + '<a rel="next" href="?page=2">Next</a>',
    ],
)
def test_listing_parser_does_not_infer_empty_from_missing_package_links(
    html: str,
) -> None:
    """Missing links, contradictory markers, and partial HTML remain unknown."""

    with pytest.raises(PackageDiscoveryError, match="unrecognized package listing"):
        parse_package_listing_html(html, PackageListingRequest("orgs", "example", 1, 0))


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

    container = (
        "user-packages-list"
        if listing_request.owner_type == "users"
        else "org-packages"
    )
    client = FakeGitHubClient(text_values={expected_url: _empty_listing(container)})

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


def test_listing_404_stays_unavailable_when_the_owner_still_exists() -> None:
    """An existing owner with no listing cannot be classified as empty."""

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


@pytest.mark.parametrize("mode", [0, 2])
def test_unrecognized_listing_uses_bounded_api_checks_to_prove_empty(mode: int) -> None:
    """Every supported ecosystem must report an empty first API page."""

    request = PackageListingRequest("orgs", "example", 1, mode)
    paths = _empty_probe_paths(request)
    client = FakeGitHubClient(
        rest_values={path: [] for path in paths},
        text_values={request.url(): "<div>changed upstream markup</div>"},
    )

    fetched = fetch_package_listing_page(client, request, verify_empty_with_api=True)

    assert fetched.page == PackageListingPage((), False)
    assert not fetched.owner_missing
    assert not fetched.listing_unavailable
    assert client.rest_requests == list(paths)
    assert "API" in fetched.diagnostic


@pytest.mark.parametrize("payload", [[{"name": "package"}], None, {"message": "oops"}])
def test_inconclusive_api_check_does_not_complete_an_unrecognized_listing(
    payload: object,
) -> None:
    """A nonempty, missing, or malformed API response cannot prove emptiness."""

    request = PackageListingRequest("users", "example", 1, 0)
    first_path = _empty_probe_paths(request)[0]
    client = FakeGitHubClient(
        rest_values={first_path: payload},
        text_values={request.url(): "<div></div>"},
    )

    fetched = fetch_package_listing_page(client, request, verify_empty_with_api=True)

    assert fetched.listing_unavailable
    assert "API" in fetched.diagnostic
    assert client.rest_requests == [first_path]


def test_unrecognized_later_page_does_not_use_first_page_api_emptiness() -> None:
    """The REST listing cursor cannot substitute for a later HTML page."""

    request = PackageListingRequest("orgs", "example", 2, 0)
    client = FakeGitHubClient(text_values={request.url(): "<div></div>"})

    fetched = fetch_package_listing_page(client, request, verify_empty_with_api=True)

    assert fetched.listing_unavailable
    assert not client.rest_requests


def test_nonempty_later_ecosystem_prevents_an_empty_owner_result() -> None:
    """An empty container listing does not prove that other ecosystems are empty."""

    request = PackageListingRequest("orgs", "example", 1, 0)
    paths = _empty_probe_paths(request)
    values: dict[str, object] = {path: [] for path in paths}
    values[paths[-1]] = [{"name": "legacy-image"}]
    client = FakeGitHubClient(
        rest_values=values,
        text_values={request.url(): "<div></div>"},
    )

    fetched = fetch_package_listing_page(client, request, verify_empty_with_api=True)

    assert fetched.listing_unavailable
    assert "nonempty inventory for docker" in fetched.diagnostic
    assert client.rest_requests == list(paths)


def test_recognized_public_empty_listing_does_not_spend_api_requests() -> None:
    """An explicit public empty state keeps the usual scrape-only path."""

    request = PackageListingRequest("orgs", "example", 1, 0)
    client = FakeGitHubClient(
        text_values={request.url(): _empty_listing("org-packages")}
    )

    fetched = fetch_package_listing_page(client, request, verify_empty_with_api=True)

    assert not fetched.listing_unavailable
    assert not client.rest_requests


@pytest.mark.parametrize("mode", [4, 5])
def test_private_capable_empty_first_page_requires_visibility_matching_api_checks(
    mode: int,
) -> None:
    """Public HTML alone does not establish the absence of readable private work."""

    request = PackageListingRequest("orgs", "example", 1, mode)
    paths = _empty_probe_paths(request)
    client = FakeGitHubClient(
        rest_values={path: [] for path in paths},
        text_values={request.url(): _empty_listing("org-packages")},
    )

    fetched = fetch_package_listing_page(client, request, verify_empty_with_api=True)

    assert not fetched.listing_unavailable
    assert client.rest_requests == list(paths)


def test_recognized_private_terminal_page_preserves_html_pagination() -> None:
    """A later zero-results page ends an already recognized private-capable scan."""

    request = PackageListingRequest("orgs", "example", 2, 4)
    client = FakeGitHubClient(
        text_values={request.url(): _empty_listing("org-packages")}
    )

    fetched = fetch_package_listing_page(client, request, verify_empty_with_api=True)

    assert not fetched.listing_unavailable
    assert not client.rest_requests
