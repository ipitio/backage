"""Tests for pooled package-version candidate loading."""

from __future__ import annotations

from collections.abc import Mapping

import httpx
import pytest

from bkg_py.github import GitHubError, GitHubJsonResponse, GitHubTextRequestPolicy
from bkg_py.version_ingestion import (
    VersionCandidateLoader,
    VersionIngestionError,
)
from bkg_py.version_selection import VersionSelectionSettings
from bkg_py.versions import VersionListingContext


class _FakePageClient:
    def __init__(
        self,
        *,
        rest_values: Mapping[str, object | Exception] | None = None,
        text_values: Mapping[str, str | Exception] | None = None,
    ) -> None:
        self.rest_values = dict(rest_values or {})
        self.text_values = dict(text_values or {})
        self.rest_requests: list[str] = []
        self.text_requests: list[str] = []

    def rest_json(self, path: str) -> GitHubJsonResponse:
        """Return one configured REST value or failure."""

        self.rest_requests.append(path)
        value = self.rest_values[path]
        if isinstance(value, Exception):
            raise value
        return GitHubJsonResponse(value=value, headers=httpx.Headers())

    def get_text(
        self,
        url: str,
        *,
        authenticated: bool = False,
        accept: str = "text/html",
        policy: GitHubTextRequestPolicy | None = None,
    ) -> str:
        """Return one configured public HTML value or failure."""

        assert not authenticated
        assert accept == "text/html"
        assert policy is None
        self.text_requests.append(url)
        value = self.text_values[url]
        if isinstance(value, Exception):
            raise value
        return value


_CONTEXT = VersionListingContext(
    owner_type="orgs",
    owner="Lazztech",
    repo="Libre-Closet",
    package_type="container",
    package="libre-closet",
)
_API_PAGE_1 = (
    "orgs/Lazztech/packages/container/libre-closet/versions?per_page=30&page=1"
)
_API_PAGE_2 = (
    "orgs/Lazztech/packages/container/libre-closet/versions?per_page=30&page=2"
)
_HTML_PAGE_1 = (
    "https://github.com/orgs/Lazztech/packages/container/libre-closet/versions?page=1"
)
_TAGGED_PAGE_1 = (
    "https://github.com/orgs/Lazztech/packages/container/"
    "libre-closet/versions?filters%5Bversion_type%5D=tagged&page=1"
)


def _api_candidates(start: int, stop: int) -> list[dict[str, object]]:
    return [
        {"id": version_id, "name": f"sha256:{version_id}", "tags": []}
        for version_id in range(start, stop, -1)
    ]


def _listing_html(*version_ids: int, tagged: bool = False) -> str:
    rows: list[str] = []
    for version_id in version_ids:
        prefix = f"/orgs/Lazztech/packages/container/libre-closet/{version_id}"
        tag_link = f'<a href="{prefix}?tag=tag-{version_id}"></a>' if tagged else ""
        rows.append(
            '<li class="Box-row">'
            f'{tag_link}<a href="{prefix}">sha256:{version_id}</a>'
            "</li>"
        )
    return "".join(rows)


def test_loader_uses_rest_and_fetches_tagged_html_only_when_needed() -> None:
    """One client supplies API candidates and a lazily requested tagged page."""

    client = _FakePageClient(
        rest_values={_API_PAGE_1: _api_candidates(10, 4)},
        text_values={_TAGGED_PAGE_1: _listing_html(5, tagged=True)},
    )
    loader = VersionCandidateLoader(client, _CONTEXT, use_rest_api=True)

    result = loader.select(
        VersionSelectionSettings(max_tag_pages=1, append_tagged_limit=0)
    )

    assert result.selected_ids == ("10", "9", "8", "7", "6", "5")
    assert result.candidates[-1].tags == ("tag-5",)
    assert client.rest_requests == [_API_PAGE_1]
    assert client.text_requests == [_TAGGED_PAGE_1]


def test_loader_falls_back_to_html_for_unusable_api_data() -> None:
    """An empty API page uses the existing public HTML fallback."""

    diagnostics: list[str] = []
    client = _FakePageClient(
        rest_values={_API_PAGE_1: []},
        text_values={_HTML_PAGE_1: _listing_html(3, 2)},
    )
    loader = VersionCandidateLoader(
        client,
        _CONTEXT,
        use_rest_api=True,
        diagnostic=diagnostics.append,
    )

    result = loader.select(
        VersionSelectionSettings(max_tag_pages=0, append_tagged_limit=0)
    )

    assert result.selected_ids == ("3", "2")
    assert client.text_requests == [_HTML_PAGE_1]
    assert diagnostics == [
        "Version API page 1 returned unusable data; falling back to HTML"
    ]


def test_loader_accepts_an_empty_later_api_page_as_the_end() -> None:
    """An empty REST page after page one ends pagination without HTML work."""

    diagnostics: list[str] = []
    client = _FakePageClient(
        rest_values={
            _API_PAGE_1: _api_candidates(30, 0),
            _API_PAGE_2: [],
        },
        text_values={},
    )
    loader = VersionCandidateLoader(
        client,
        _CONTEXT,
        use_rest_api=True,
        diagnostic=diagnostics.append,
    )

    result = loader.select(
        VersionSelectionSettings(max_tag_pages=0, append_tagged_limit=0)
    )

    assert len(result.candidates) == 30
    assert client.rest_requests == [_API_PAGE_1, _API_PAGE_2]
    assert not client.text_requests
    assert not diagnostics


def test_loader_falls_back_to_html_after_api_failure() -> None:
    """A REST failure remains recoverable when the HTML listing is available."""

    diagnostics: list[str] = []
    client = _FakePageClient(
        rest_values={_API_PAGE_1: GitHubError("temporary API failure")},
        text_values={_HTML_PAGE_1: _listing_html(3)},
    )
    loader = VersionCandidateLoader(
        client,
        _CONTEXT,
        use_rest_api=True,
        diagnostic=diagnostics.append,
    )

    result = loader.select(
        VersionSelectionSettings(max_tag_pages=0, append_tagged_limit=0)
    )

    assert result.selected_ids == ("3",)
    assert "temporary API failure" in diagnostics[0]
    assert client.text_requests == [_HTML_PAGE_1]


def test_loader_honors_page_limit_without_eager_html_fetches() -> None:
    """A one-page limit does not consume a second available HTML page."""

    first_page = _listing_html(*range(30, 0, -1))
    client = _FakePageClient(text_values={_HTML_PAGE_1: first_page})
    loader = VersionCandidateLoader(client, _CONTEXT, use_rest_api=False)

    result = loader.select(
        VersionSelectionSettings(
            max_version_pages=1,
            max_tag_pages=0,
            append_tagged_limit=0,
        )
    )

    assert result.version_pages_read == 1
    assert len(result.selected_ids) == 30
    assert not client.rest_requests
    assert client.text_requests == [_HTML_PAGE_1]


def test_loader_reports_html_transport_failure() -> None:
    """A failed final listing source is not mistaken for an empty package."""

    client = _FakePageClient(
        text_values={_HTML_PAGE_1: GitHubError("HTML unavailable")}
    )
    loader = VersionCandidateLoader(client, _CONTEXT, use_rest_api=False)

    with pytest.raises(
        VersionIngestionError,
        match="failed to fetch version listing for Lazztech/libre-closet",
    ):
        loader.select(VersionSelectionSettings())
