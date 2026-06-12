"""Tests for the pooled GitHub HTTP client."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from bkg_py.github import (
    GitHubClient,
    GitHubDecodeError,
    GitHubGraphQLError,
    GitHubRateAccounting,
    GitHubResponseError,
    GitHubRuntime,
    GitHubSettings,
    GitHubTransportError,
)
from bkg_py.runtime import GracefulStop
from bkg_py.state import StateStore

TEST_TOKEN = "github_pat_secret"


def _settings(**overrides: object) -> GitHubSettings:
    values: dict[str, object] = {
        "token": TEST_TOKEN,
        "total_timeout": 30,
        "initial_backoff": 1,
        "max_backoff": 8,
    }
    values.update(overrides)
    return GitHubSettings(**values)  # type: ignore[arg-type]


def _client(
    handler: httpx.MockTransport,
    *,
    settings: GitHubSettings | None = None,
    accounting: GitHubRateAccounting | None = None,
    runtime: GitHubRuntime | None = None,
) -> GitHubClient:
    return GitHubClient(
        settings or _settings(),
        accounting=accounting,
        runtime=runtime,
        client=httpx.Client(transport=handler),
    )


def test_rest_success_authentication_and_accounting(tmp_path: Path) -> None:
    """REST requests authenticate and persist header-based usage."""

    state_path = tmp_path / "env.env"
    state_path.touch()
    state = StateStore(state_path)

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://api.github.com/users/ipitio"
        assert request.headers["authorization"] == f"Bearer {TEST_TOKEN}"
        assert request.headers["x-github-api-version"] == "2022-11-28"
        return httpx.Response(
            200,
            json={"login": "ipitio"},
            headers={
                "x-ratelimit-remaining": "4999",
                "x-ratelimit-reset": "1781139600",
            },
        )

    client = _client(
        httpx.MockTransport(respond),
        accounting=GitHubRateAccounting(state),
    )
    response = client.rest_json("users/ipitio")

    assert response.value == {"login": "ipitio"}
    assert state.get_int("BKG_CALLS_TO_API") == 1
    assert state.get_int("BKG_MIN_CALLS_TO_API") == 1
    assert state.get("BKG_REST_REMAINING") == "4999"
    assert state.get("BKG_REST_RESET_AT") == "1781139600"


def test_graphql_injects_and_accounts_for_reported_rate_cost(
    tmp_path: Path,
) -> None:
    """GraphQL requests include and persist GitHub's actual query cost."""

    state_path = tmp_path / "env.env"
    state_path.write_text(
        "BKG_CALLS_TO_API=5\nBKG_MIN_CALLS_TO_API=7\n\n",
        encoding="utf-8",
    )
    state = StateStore(state_path)

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert "rateLimit { cost remaining resetAt }" in payload["query"]
        return httpx.Response(
            200,
            json={
                "data": {
                    "viewer": {"login": "ipitio"},
                    "rateLimit": {
                        "cost": 17,
                        "remaining": 4321,
                        "resetAt": "2026-06-11T23:59:59Z",
                    },
                }
            },
            headers={
                "x-ratelimit-remaining": "4998",
                "x-ratelimit-reset": "1781139600",
            },
        )

    client = _client(
        httpx.MockTransport(respond),
        accounting=GitHubRateAccounting(state),
    )
    response = client.graphql("query { viewer { login } }")

    assert response.value["data"]["viewer"]["login"] == "ipitio"
    assert state.get_int("BKG_CALLS_TO_API") == 22
    assert state.get_int("BKG_MIN_CALLS_TO_API") == 24
    assert state.get("BKG_GRAPHQL_LAST_COST") == "17"
    assert state.get("BKG_GRAPHQL_REMAINING") == "4321"
    assert state.get("BKG_GRAPHQL_RESET_AT") == "2026-06-11T23:59:59Z"
    assert state.get("BKG_REST_REMAINING") == "4998"


def test_rest_pagination_reuses_client() -> None:
    """Pagination follows next links through one reusable client."""

    requests: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        page = request.url.params.get("page", "1")
        headers: dict[str, str] = {}
        if page == "1":
            headers["link"] = (
                '<https://api.github.com/items?page=2>; rel="next", '
                '<https://api.github.com/items?page=2>; rel="last"'
            )
        return httpx.Response(200, json=[int(page)], headers=headers)

    client = _client(httpx.MockTransport(respond))

    assert [page.value for page in client.rest_pages("items?page=1")] == [[1], [2]]
    assert requests == [
        "https://api.github.com/items?page=1",
        "https://api.github.com/items?page=2",
    ]


def test_optional_rest_returns_none_only_for_not_found() -> None:
    """Callers can explicitly treat HTTP 404 as an absent resource."""

    client = _client(httpx.MockTransport(lambda _request: httpx.Response(404, json={})))

    assert client.rest_json_optional("users/missing") is None
    with pytest.raises(GitHubResponseError, match="HTTP 404"):
        client.rest_json("users/missing")


def test_graphql_errors_are_not_treated_as_missing_data() -> None:
    """A successful HTTP response with GraphQL errors still fails."""

    client = _client(
        httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={
                    "data": {"owner": None},
                    "errors": [{"message": "Something went wrong"}],
                },
            )
        )
    )

    with pytest.raises(GitHubGraphQLError, match="Something went wrong"):
        client.graphql('query { owner: repositoryOwner(login: "example") { id } }')


def test_transient_response_retries_with_exponential_backoff(
    tmp_path: Path,
) -> None:
    """Transient server failures use bounded exponential backoff."""

    attempts = 0
    sleeps: list[float] = []
    state_path = tmp_path / "env.env"
    state_path.touch()
    state = StateStore(state_path)

    def respond(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return httpx.Response(503, text="try later")
        return httpx.Response(200, json={"ok": True})

    client = _client(
        httpx.MockTransport(respond),
        accounting=GitHubRateAccounting(state),
        runtime=GitHubRuntime(sleep=sleeps.append),
    )

    assert client.rest_json("example").value == {"ok": True}
    assert attempts == 3
    assert sleeps == [1, 2]
    assert state.get_int("BKG_CALLS_TO_API") == 3
    assert state.get_int("BKG_MIN_CALLS_TO_API") == 3


def test_secondary_rate_limit_honors_retry_after() -> None:
    """Secondary limits use GitHub's explicit retry delay."""

    attempts = 0
    sleeps: list[float] = []

    def respond(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(
                403,
                json={"message": "You have exceeded a secondary rate limit."},
                headers={"retry-after": "7"},
            )
        return httpx.Response(200, json={"ok": True})

    client = _client(
        httpx.MockTransport(respond),
        runtime=GitHubRuntime(sleep=sleeps.append),
    )

    assert client.rest_json("example").value == {"ok": True}
    assert sleeps == [7]


def test_malformed_json_has_clear_error() -> None:
    """Successful responses with malformed JSON fail clearly."""

    client = _client(
        httpx.MockTransport(
            lambda _request: httpx.Response(200, text="<html>not json</html>")
        )
    )

    with pytest.raises(GitHubDecodeError, match="invalid JSON"):
        client.rest_json("example")


def test_stop_interrupts_retry_sleep() -> None:
    """Graceful-stop requests interrupt retry waiting."""

    def stop_sleep(_seconds: float) -> None:
        raise GracefulStop("test")

    client = _client(
        httpx.MockTransport(lambda _request: httpx.Response(503, text="try later")),
        runtime=GitHubRuntime(sleep=stop_sleep),
    )

    with pytest.raises(GracefulStop):
        client.rest_json("example")


def test_errors_and_settings_repr_redact_token() -> None:
    """Tokens do not appear in settings or response errors."""

    settings = _settings()

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text=f"token {TEST_TOKEN}", request=request)

    client = _client(httpx.MockTransport(respond), settings=settings)

    with pytest.raises(GitHubResponseError) as raised:
        client.rest_json(f"example?token={TEST_TOKEN}")
    assert TEST_TOKEN not in str(raised.value)
    assert TEST_TOKEN not in repr(settings)
    assert "[REDACTED]" in str(raised.value)


def test_total_timeout_stops_retry_sequence() -> None:
    """The operation deadline bounds the entire retry sequence."""

    clock_value = 0.0

    def clock() -> float:
        return clock_value

    def sleep(seconds: float) -> None:
        nonlocal clock_value
        clock_value += seconds

    client = _client(
        httpx.MockTransport(lambda _request: httpx.Response(503, text="try later")),
        settings=_settings(total_timeout=1.5),
        runtime=GitHubRuntime(sleep=sleep, clock=clock),
    )

    with pytest.raises(GitHubTransportError, match="total timeout"):
        client.rest_json("example")


def test_external_download_is_atomic_and_unauthenticated(tmp_path: Path) -> None:
    """External downloads preserve atomicity and do not leak credentials."""

    destination = tmp_path / "asset.db"
    destination.write_bytes(b"old")

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://objects.example/asset.db"
        assert "authorization" not in request.headers
        return httpx.Response(200, content=b"new snapshot")

    client = _client(httpx.MockTransport(respond))
    client.download("https://objects.example/asset.db", destination)

    assert destination.read_bytes() == b"new snapshot"
    assert not list(tmp_path.glob(".asset.db.*"))


def test_interrupted_download_preserves_destination(tmp_path: Path) -> None:
    """Interrupted downloads leave the previous complete file in place."""

    destination = tmp_path / "asset.db"
    destination.write_bytes(b"old")
    checks = 0

    def check_stop() -> None:
        nonlocal checks
        checks += 1
        if checks >= 2:
            raise GracefulStop("test")

    client = _client(
        httpx.MockTransport(lambda _request: httpx.Response(200, content=b"partial")),
        runtime=GitHubRuntime(check_stop=check_stop),
    )

    with pytest.raises(GracefulStop):
        client.download("https://objects.example/asset.db", destination)
    assert destination.read_bytes() == b"old"
