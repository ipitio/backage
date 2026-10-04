"""Tests for pooled package-version candidate loading."""

from collections.abc import Mapping

import httpx
import pytest

from bkg_py.github import (
    GitHubError,
    GitHubJsonResponse,
    GitHubTextRequestPolicy,
    GitHubTransportError,
)
from bkg_py.packages.enrichment import RequestCircuit, RequestCircuitSettings
from bkg_py.packages.versions.ingestion import (
    VersionCandidateLoader,
    VersionCandidateLoaderSettings,
    VersionIngestionError,
    VersionListingUnavailable,
)
from bkg_py.packages.versions.metadata import VersionListingContext
from bkg_py.packages.versions.selection import VersionSelectionSettings


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

_LEGACY_CONTEXT = VersionListingContext(
    "orgs", "Example", "repo", "maven", "org.example.library", "12345"
)
_LEGACY_API_PAGE = (
    "orgs/Example/packages/maven/org.example.library/versions?per_page=30&page=1"
)
_LEGACY_API_PACKAGE = "orgs/Example/packages/maven/org.example.library"
_LEGACY_IDENTITY = {
    "id": 12345,
    "package_type": "maven",
    "name": "org.example.library",
    "repository": {"full_name": "Example/repo"},
}


def test_legacy_versions_require_authoritative_ids_but_no_tagged_scrape() -> None:
    """Only ID discovery uses REST, even when ordinary enrichment prefers HTML."""

    client = _FakePageClient(
        rest_values={
            _LEGACY_API_PACKAGE: _LEGACY_IDENTITY,
            _LEGACY_API_PAGE: [{"id": 7, "name": "1.2.3"}],
        }
    )
    result = VersionCandidateLoader(
        client, _LEGACY_CONTEXT, VersionCandidateLoaderSettings(use_rest_api=False)
    ).select(VersionSelectionSettings())

    assert result.selected_ids == ("7",)
    assert not result.used_fallback
    assert result.candidates[0].name == "1.2.3"
    assert result.tag_pages_read == 0
    assert client.rest_requests == [_LEGACY_API_PACKAGE, _LEGACY_API_PAGE]
    assert not client.text_requests


@pytest.mark.parametrize(
    "response",
    [
        [],
        {"message": "unavailable"},
        [{"id": -1, "name": "1.2.3"}],
        [{"id": 0, "name": "1.2.3"}],
        [{"id": 7}],
        [{"id": 7, "name": {"invalid": "version"}}],
        [{"id": 7, "name": "1"}, {"id": 7, "name": "2"}],
        GitHubError("package versions API unavailable"),
    ],
)
def test_unavailable_legacy_versions_do_not_generate_an_anonymous_record(
    response: object,
) -> None:
    """Unknown inventory stays retryable instead of falling back to ID -1."""

    client = _FakePageClient(
        rest_values={
            _LEGACY_API_PACKAGE: _LEGACY_IDENTITY,
            _LEGACY_API_PAGE: response,
        }
    )
    loader = VersionCandidateLoader(
        client, _LEGACY_CONTEXT, VersionCandidateLoaderSettings(use_rest_api=False)
    )
    with pytest.raises(VersionListingUnavailable, match="stable numeric IDs"):
        loader.select(VersionSelectionSettings())
    assert not client.text_requests


@pytest.mark.parametrize(
    "identity",
    [
        {**_LEGACY_IDENTITY, "id": 54321},
        {**_LEGACY_IDENTITY, "package_type": "container"},
        {**_LEGACY_IDENTITY, "repository": {"full_name": "Example/other"}},
        GitHubError("package identity is not readable"),
    ],
)
def test_legacy_identity_must_match_before_requesting_versions(
    identity: object,
) -> None:
    """An API's namesake in another repository cannot supply this package's IDs."""

    client = _FakePageClient(rest_values={_LEGACY_API_PACKAGE: identity})
    loader = VersionCandidateLoader(
        client, _LEGACY_CONTEXT, VersionCandidateLoaderSettings(use_rest_api=False)
    )
    with pytest.raises(VersionListingUnavailable, match="package identity"):
        loader.select(VersionSelectionSettings())
    assert client.rest_requests == [_LEGACY_API_PACKAGE]
    assert not client.text_requests


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
    loader = VersionCandidateLoader(
        client, _CONTEXT, VersionCandidateLoaderSettings(use_rest_api=True)
    )

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
        VersionCandidateLoaderSettings(
            use_rest_api=True,
            diagnostic=diagnostics.append,
        ),
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
        VersionCandidateLoaderSettings(
            use_rest_api=True,
            diagnostic=diagnostics.append,
        ),
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
        VersionCandidateLoaderSettings(
            use_rest_api=True,
            diagnostic=diagnostics.append,
        ),
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
    loader = VersionCandidateLoader(
        client, _CONTEXT, VersionCandidateLoaderSettings(use_rest_api=False)
    )

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
    loader = VersionCandidateLoader(
        client, _CONTEXT, VersionCandidateLoaderSettings(use_rest_api=False)
    )

    with pytest.raises(
        VersionIngestionError,
        match="failed to fetch version listing for Lazztech/libre-closet",
    ):
        loader.select(VersionSelectionSettings())


def test_loader_pauses_api_failures_without_disabling_html_fallback() -> None:
    """Repeated API failures open only the API listing circuit."""

    diagnostics: list[str] = []
    client = _FakePageClient(
        rest_values={_API_PAGE_1: GitHubTransportError("API unavailable")},
        text_values={_HTML_PAGE_1: _listing_html(3)},
    )
    recovery = RequestCircuit(
        RequestCircuitSettings(
            max_concurrent=1,
            failure_threshold=2,
            cooldown_seconds=30,
        )
    )

    for _index in range(3):
        result = VersionCandidateLoader(
            client,
            _CONTEXT,
            VersionCandidateLoaderSettings(
                use_rest_api=True,
                diagnostic=diagnostics.append,
            ),
            request_recovery=recovery,
        ).select(VersionSelectionSettings(max_tag_pages=0, append_tagged_limit=0))
        assert result.selected_ids == ("3",)

    assert client.rest_requests == [_API_PAGE_1, _API_PAGE_1]
    assert client.text_requests == [_HTML_PAGE_1, _HTML_PAGE_1, _HTML_PAGE_1]
    assert (
        sum("Pausing GitHub version-listing API" in line for line in diagnostics) == 1
    )


def test_loader_pauses_html_failures_without_repeating_requests() -> None:
    """An open HTML circuit preserves data without another network attempt."""

    diagnostics: list[str] = []
    client = _FakePageClient(
        text_values={_HTML_PAGE_1: GitHubTransportError("HTML unavailable")}
    )
    recovery = RequestCircuit(
        RequestCircuitSettings(
            max_concurrent=1,
            failure_threshold=2,
            cooldown_seconds=30,
        )
    )

    for _index in range(2):
        loader = VersionCandidateLoader(
            client,
            _CONTEXT,
            VersionCandidateLoaderSettings(
                use_rest_api=False,
                diagnostic=diagnostics.append,
            ),
            request_recovery=recovery,
        )
        with pytest.raises(VersionIngestionError, match="HTML unavailable"):
            loader.select(VersionSelectionSettings())

    loader = VersionCandidateLoader(
        client,
        _CONTEXT,
        VersionCandidateLoaderSettings(
            use_rest_api=False,
            diagnostic=diagnostics.append,
        ),
        request_recovery=recovery,
    )
    with pytest.raises(VersionListingUnavailable, match="temporarily paused"):
        loader.select(VersionSelectionSettings())

    assert client.text_requests == [_HTML_PAGE_1, _HTML_PAGE_1]
    assert (
        sum("Pausing GitHub version-listing HTML" in line for line in diagnostics) == 1
    )
