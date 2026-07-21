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
    GitHubTextRequestPolicy,
    GitHubTransportError,
)
from bkg_py.runtime import GracefulStop
from bkg_py.state import StateStore

TEST_TOKEN = "github_pat_secret"
TEST_REGISTRY_TOKEN = "registry-token"


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
                "x-ratelimit-limit": "5000",
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
    client.close()
    assert state.get_int("BKG_CALLS_TO_API") == 1
    assert state.get_int("BKG_MIN_CALLS_TO_API") == 1
    assert state.get("BKG_REST_LIMIT") == "5000"
    assert state.get("BKG_REST_REMAINING") == "4999"
    assert state.get("BKG_REST_RESET_AT") == "1781139600"


def test_rest_delete_uses_shared_authentication() -> None:
    """No-content REST mutations use the same authenticated transport."""

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url == "https://api.github.com/repos/example/bkg/releases/42"
        assert request.headers["authorization"] == f"Bearer {TEST_TOKEN}"
        return httpx.Response(204)

    client = _client(httpx.MockTransport(respond))

    client.rest_delete("repos/example/bkg/releases/42")


def test_rate_accounting_flushes_in_bounded_response_batches(
    tmp_path: Path,
) -> None:
    """Frequent responses do not force one durable state rewrite each."""

    state = StateStore(tmp_path / "env.env")
    accounting = GitHubRateAccounting(state, flush_responses=3)

    accounting.record_rest({}, budgeted=False)
    accounting.record_rest({}, budgeted=False)
    assert state.get_int("BKG_CALLS_TO_API") == 0

    accounting.record_rest({}, budgeted=False)

    assert state.get_int("BKG_CALLS_TO_API") == 3
    assert state.get_int("BKG_MIN_CALLS_TO_API") == 3


def test_text_request_retries_without_api_headers_or_accounting(
    tmp_path: Path,
) -> None:
    """HTML requests reuse retry behavior without consuming REST accounting."""

    attempts = 0
    sleeps: list[float] = []
    state_path = tmp_path / "env.env"
    state_path.touch()
    state = StateStore(state_path)

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        assert request.url == "https://github.com/example/pkg/versions"
        assert request.headers["accept"] == "text/html"
        assert "authorization" not in request.headers
        assert "x-github-api-version" not in request.headers
        if attempts == 1:
            return httpx.Response(503, headers={"retry-after": "0.25"})
        return httpx.Response(200, text="<html>versions</html>")

    client = _client(
        httpx.MockTransport(respond),
        accounting=GitHubRateAccounting(state),
        runtime=GitHubRuntime(sleep=sleeps.append),
    )

    assert (
        client.get_text("https://github.com/example/pkg/versions")
        == "<html>versions</html>"
    )
    assert attempts == 2
    assert sleeps == [0.25]
    assert state.get_int("BKG_CALLS_TO_API") == 0


def test_text_request_policy_can_disable_retries_for_optional_work() -> None:
    """Optional enrichment can fail quickly without changing global HTTP policy."""

    attempts = 0

    def respond(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(503, text="try later")

    client = _client(httpx.MockTransport(respond))

    with pytest.raises(GitHubResponseError, match="HTTP 503"):
        client.get_text(
            "https://github.com/example/pkg/versions",
            policy=GitHubTextRequestPolicy(total_timeout=30, max_attempts=1),
        )
    assert attempts == 1


def test_text_request_accepts_an_explicit_registry_bearer_token() -> None:
    """Non-API text requests can use a short-lived registry pull token."""

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://ghcr.io/v2/example/pkg/manifests/latest"
        assert request.headers["authorization"] == f"Bearer {TEST_REGISTRY_TOKEN}"
        assert request.headers["accept"] == "application/vnd.oci.image.manifest.v1+json"
        assert "x-github-api-version" not in request.headers
        return httpx.Response(200, text='{"layers":[]}')

    client = _client(httpx.MockTransport(respond))

    assert (
        client.get_text(
            "https://ghcr.io/v2/example/pkg/manifests/latest",
            accept="application/vnd.oci.image.manifest.v1+json",
            bearer_token=TEST_REGISTRY_TOKEN,
        )
        == '{"layers":[]}'
    )


def test_graphql_injects_and_accounts_for_reported_rate_cost(
    tmp_path: Path,
) -> None:
    """GraphQL requests include and persist GitHub's actual query cost."""

    state_path = tmp_path / "env.env"
    state_path.write_text(
        "BKG_CALLS_TO_API=5\n"
        "BKG_MIN_CALLS_TO_API=7\n"
        "BKG_REST_REMAINING=321\n"
        "BKG_REST_RESET_AT=1781139500\n\n",
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
    client.close()
    assert state.get_int("BKG_CALLS_TO_API") == 22
    assert state.get_int("BKG_MIN_CALLS_TO_API") == 24
    assert state.get("BKG_GRAPHQL_LAST_COST") == "17"
    assert state.get("BKG_GRAPHQL_REMAINING") == "4321"
    assert state.get("BKG_GRAPHQL_RESET_AT") == "2026-06-11T23:59:59Z"
    assert state.get("BKG_REST_REMAINING") == "321"
    assert state.get("BKG_REST_RESET_AT") == "1781139500"


def test_authenticated_rest_waits_at_workflow_reserve(tmp_path: Path) -> None:
    """REST work waits for reset instead of consuming publication capacity."""

    state_path = tmp_path / "env.env"
    state_path.write_text(
        "BKG_REST_REMAINING=50\nBKG_REST_RESET_AT=1100\n\n",
        encoding="utf-8",
    )
    state = StateStore(state_path)
    accounting = GitHubRateAccounting(state, rest_reserve=50)
    monotonic = 0.0
    wall_clock = 1000.0
    sleeps: list[float] = []
    reports: list[str] = []

    def sleep(seconds: float) -> None:
        nonlocal monotonic, wall_clock
        sleeps.append(seconds)
        monotonic += seconds
        wall_clock += seconds

    client = _client(
        httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={"ok": True},
                headers={
                    "x-ratelimit-limit": "1000",
                    "x-ratelimit-remaining": "999",
                    "x-ratelimit-reset": "4700",
                },
            )
        ),
        accounting=accounting,
        runtime=GitHubRuntime(
            sleep=sleep,
            clock=lambda: monotonic,
            wall_clock=lambda: wall_clock,
            report=reports.append,
        ),
    )

    assert client.rest_json("example").value == {"ok": True}
    assert sleeps == [101]
    assert "50-request workflow reserve" in reports[0]
    assert reports[1] == "GitHub REST budget reset; resuming API work"
    client.close()
    assert state.get("BKG_REST_REMAINING") == "999"


def test_rest_reserve_stop_interrupts_wait_without_request(tmp_path: Path) -> None:
    """An elapsed stop during a rate wait leaves the reserved calls unused."""

    state_path = tmp_path / "env.env"
    state_path.write_text(
        "BKG_REST_REMAINING=50\nBKG_REST_RESET_AT=2000\n\n",
        encoding="utf-8",
    )
    requests = 0

    def respond(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, json={"ok": True})

    def stop_sleep(_seconds: float) -> None:
        raise GracefulStop("elapsed")

    client = _client(
        httpx.MockTransport(respond),
        accounting=GitHubRateAccounting(StateStore(state_path), rest_reserve=50),
        runtime=GitHubRuntime(
            sleep=stop_sleep,
            wall_clock=lambda: 1000,
        ),
    )

    with pytest.raises(GracefulStop, match="elapsed"):
        client.rest_json("example")
    assert requests == 0


def test_primary_limit_wait_renews_operation_deadline(tmp_path: Path) -> None:
    """A primary-limit reset wait does not consume the request retry deadline."""

    state_path = tmp_path / "env.env"
    state_path.touch()
    attempts = 0
    monotonic = 0.0
    wall_clock = 1000.0

    def respond(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(
                403,
                json={"message": "API rate limit exceeded"},
                headers={
                    "x-ratelimit-limit": "1000",
                    "x-ratelimit-remaining": "0",
                    "x-ratelimit-reset": "1100",
                },
            )
        return httpx.Response(
            200,
            json={"ok": True},
            headers={
                "x-ratelimit-limit": "1000",
                "x-ratelimit-remaining": "999",
                "x-ratelimit-reset": "4700",
            },
        )

    def sleep(seconds: float) -> None:
        nonlocal monotonic, wall_clock
        monotonic += seconds
        wall_clock += seconds

    client = _client(
        httpx.MockTransport(respond),
        settings=_settings(total_timeout=30),
        accounting=GitHubRateAccounting(StateStore(state_path), rest_reserve=50),
        runtime=GitHubRuntime(
            sleep=sleep,
            clock=lambda: monotonic,
            wall_clock=lambda: wall_clock,
        ),
    )

    assert client.rest_json("example").value == {"ok": True}
    assert attempts == 2
    assert monotonic == 101


def test_rest_reservations_include_concurrent_in_flight_requests(
    tmp_path: Path,
) -> None:
    """Workers cannot all spend the same last reported REST capacity."""

    state_path = tmp_path / "env.env"
    state_path.write_text(
        "BKG_REST_REMAINING=52\nBKG_REST_RESET_AT=2000\n\n",
        encoding="utf-8",
    )
    accounting = GitHubRateAccounting(StateStore(state_path), rest_reserve=50)

    assert accounting.reserve_rest(1000) is None
    assert accounting.reserve_rest(1000) is None
    wait = accounting.reserve_rest(1000)
    assert wait is not None
    assert wait.seconds == 1001

    accounting.cancel_rest()
    assert accounting.reserve_rest(1000) is None


def test_unauthenticated_rest_does_not_consume_token_reserve(tmp_path: Path) -> None:
    """Public fallback REST calls stay separate from the workflow token budget."""

    state_path = tmp_path / "env.env"
    state_path.write_text(
        "BKG_REST_REMAINING=50\nBKG_REST_RESET_AT=2000\n\n",
        encoding="utf-8",
    )
    state = StateStore(state_path)
    client = _client(
        httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={"ok": True},
                headers={
                    "x-ratelimit-remaining": "2",
                    "x-ratelimit-reset": "1900",
                },
            )
        ),
        accounting=GitHubRateAccounting(state, rest_reserve=50),
        runtime=GitHubRuntime(wall_clock=lambda: 1000),
    )

    assert client.rest_json("example", authenticated=False).value == {"ok": True}
    client.close()
    assert state.get("BKG_REST_REMAINING") == "50"
    assert state.get_int("BKG_CALLS_TO_API") == 1


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
    with pytest.raises(GitHubResponseError, match="HTTP 404") as captured:
        client.rest_json("users/missing")
    assert captured.value.status_code == 404


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
    client.close()
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


def test_html_error_response_uses_compact_title() -> None:
    """Transient HTML failure pages do not flood one-line diagnostics."""

    body = """
        <!DOCTYPE html>
        <html><head><title>Unicorn! &middot; GitHub</title>
        <style>large failure page</style></head><body>Try again</body></html>
    """
    client = _client(
        httpx.MockTransport(
            lambda _request: httpx.Response(
                502,
                text=body,
                headers={"content-type": "text/html; charset=utf-8"},
            )
        ),
        settings=_settings(max_attempts=1),
    )

    with pytest.raises(GitHubResponseError) as raised:
        client.rest_json("example")

    message = str(raised.value)
    assert "HTML response (Unicorn!" in message
    assert "large failure page" not in message


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
