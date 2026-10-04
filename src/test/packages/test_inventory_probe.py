"""Tests for bounded package inventory verification and token capabilities."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from urllib.parse import urlencode

import pytest

from bkg_py.github import GitHubJsonResponse, GitHubResponseError
from bkg_py.packages.discovery import PackageListingRequest, fetch_package_listing_page
from bkg_py.packages.inventory_probe import PackageInventoryProbe
from bkg_py.runtime import GracefulStop

from ..github.fake import FakeGitHubClient


def _invalid_argument() -> GitHubResponseError:
    return GitHubResponseError(
        "GitHub returned HTTP 400: Invalid argument.", status_code=400
    )


def test_installation_token_failure_is_shared_across_owners_but_not_runs() -> None:
    """One rejected probe stops repeated requests without inventing empty owners."""

    first = PackageListingRequest("users", "first", 1, 0)
    second = PackageListingRequest("users", "second", 1, 0)
    path = (
        "users/first/packages?package_type=container&per_page=1&page=1"
        "&visibility=public"
    )
    client = FakeGitHubClient(
        rest_values={path: _invalid_argument()},
        text_values={first.url(): "<div></div>", second.url(): "<div></div>"},
    )
    probe = PackageInventoryProbe(installation_token=True)

    for request in (first, second):
        fetched = fetch_package_listing_page(
            client, request, verify_empty_with_api=True, inventory_probe=probe
        )
        assert fetched.listing_unavailable
        assert f"{request.owner} page 1" in fetched.diagnostic
        assert "installation token with HTTP 400" in fetched.diagnostic
    assert client.rest_requests == [path]

    fresh_probe = PackageInventoryProbe(installation_token=True)
    fetched = fetch_package_listing_page(
        client, first, verify_empty_with_api=True, inventory_probe=fresh_probe
    )
    assert fetched.listing_unavailable
    assert client.rest_requests == [path, path]


@pytest.mark.parametrize(
    ("owner_type", "visibility"),
    [("orgs", "public"), ("users", "private"), ("users", None)],
)
def test_token_capability_failure_does_not_disable_other_inventory_scopes(
    owner_type: str, visibility: str | None
) -> None:
    """A rejected user/public listing cannot decide organization or private access."""

    rejected = (
        "users/first/packages?package_type=container&per_page=1&page=1"
        "&visibility=public"
    )
    paths: list[str] = []
    for kind in ("container", "npm", "maven", "rubygems", "nuget", "docker"):
        query: list[tuple[str, str | int]] = [
            ("package_type", kind),
            ("per_page", 1),
            ("page", 1),
        ]
        if visibility is not None:
            query.append(("visibility", visibility))
        paths.append(f"{owner_type}/other/packages?{urlencode(query)}")
    client = FakeGitHubClient(
        rest_values={rejected: _invalid_argument(), **{path: [] for path in paths}}
    )
    probe = PackageInventoryProbe(installation_token=True)

    assert not probe.verify_empty(client, "users", "first", visibility="public")[0]
    assert probe.verify_empty(client, owner_type, "other", visibility=visibility)[0]
    assert client.rest_requests == [rejected, *paths]


@pytest.mark.parametrize(
    ("installation_token", "status", "message"),
    [
        (False, 400, "Invalid argument."),
        (True, 400, "Other invalid request"),
        (True, 403, "Forbidden"),
        (True, 429, "Rate limited"),
        (True, 500, "Internal server error"),
    ],
)
def test_other_api_failures_do_not_poison_run_scoped_capability_memory(
    installation_token: bool, status: int, message: str
) -> None:
    """Only the known installation-token failure suppresses further API probes."""

    path = (
        "users/example/packages?package_type=container&per_page=1&page=1"
        "&visibility=public"
    )
    client = FakeGitHubClient(
        rest_values={path: GitHubResponseError(message, status_code=status)}
    )
    probe = PackageInventoryProbe(installation_token=installation_token)

    for _ in range(2):
        empty, diagnostic = probe.verify_empty(
            client, "users", "example", visibility="public"
        )
        assert not empty
        assert message in diagnostic
    assert client.rest_requests == [path, path]


def test_waiting_inventory_probe_stops_without_waiting_for_another_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A worker waiting for the shared API check still honors graceful stop."""

    entered = Event()
    release = Event()
    stop = Event()

    def check_stop() -> None:
        if stop.is_set():
            raise GracefulStop("test stop")

    def blocking_request(_path: str) -> GitHubJsonResponse | None:
        entered.set()
        assert release.wait(5)
        raise _invalid_argument()

    client = FakeGitHubClient()
    monkeypatch.setattr(client, "rest_json_optional", blocking_request)
    probe = PackageInventoryProbe(installation_token=True, check_stop=check_stop)
    with ThreadPoolExecutor(max_workers=2) as workers:
        active = workers.submit(
            probe.verify_empty, client, "users", "first", visibility="public"
        )
        try:
            assert entered.wait(5)
            stop.set()
            waiting = workers.submit(
                probe.verify_empty, client, "users", "second", visibility="public"
            )
            with pytest.raises(GracefulStop, match="test stop"):
                waiting.result(timeout=5)
        finally:
            release.set()
        assert not active.result(timeout=5)[0]

    stop.clear()
    assert not probe.verify_empty(client, "users", "third", visibility="public")[0]
