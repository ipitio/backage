"""Tests for direct GHCR manifest resolution."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from threading import Event

from bkg_py.enrichment import RequestCircuit, RequestCircuitSettings
from bkg_py.github import (
    GitHubResponseError,
    GitHubTextRequestPolicy,
)
from bkg_py.registry import (
    GHCRBadgeSizeInspector,
    GHCRManifestInspector,
    parse_badge_size,
    parse_size_value,
)
from bkg_py.versions import manifest_size


@dataclass(frozen=True)
class _Request:
    url: str
    accept: str
    bearer_token: str | None
    policy: GitHubTextRequestPolicy | None


class _FakeClient:  # pylint: disable=too-few-public-methods
    def __init__(self, responses: dict[str, list[str | Exception]]) -> None:
        self.responses = responses
        self.requests: list[_Request] = []

    def get_text(  # pylint: disable=too-many-arguments
        self,
        url: str,
        *,
        authenticated: bool = False,
        accept: str = "text/html",
        bearer_token: str | None = None,
        policy: GitHubTextRequestPolicy | None = None,
    ) -> str:
        """Return the next configured response for one registry URL."""

        assert not authenticated
        self.requests.append(_Request(url, accept, bearer_token, policy))
        response = self.responses[url].pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class _BlockingMissingClient(  # pylint: disable=too-few-public-methods
    _FakeClient
):
    def __init__(
        self,
        responses: dict[str, list[str | Exception]],
        missing_url: str,
        second_root_url: str,
    ) -> None:
        super().__init__(responses)
        self.missing_url = missing_url
        self.second_root_url = second_root_url
        self.missing_started = Event()
        self.second_root_started = Event()
        self.release_missing = Event()

    def get_text(  # pylint: disable=too-many-arguments
        self,
        url: str,
        *,
        authenticated: bool = False,
        accept: str = "text/html",
        bearer_token: str | None = None,
        policy: GitHubTextRequestPolicy | None = None,
    ) -> str:
        """Hold one missing child lookup while another caller reaches it."""

        if url == self.missing_url:
            self.requests.append(_Request(url, accept, bearer_token, policy))
            self.missing_started.set()
            assert self.release_missing.wait(timeout=5)
            raise GitHubResponseError("missing", status_code=404)
        if url == self.second_root_url:
            self.second_root_started.set()
        return super().get_text(
            url,
            authenticated=authenticated,
            accept=accept,
            bearer_token=bearer_token,
            policy=policy,
        )


def _token_url(repository: str = "example/nested/image") -> str:
    scope = repository.replace("/", "%2F")
    return f"https://ghcr.io/token?service=ghcr.io&scope=repository%3A{scope}%3Apull"


def _manifest_url(reference: str) -> str:
    return f"https://ghcr.io/v2/example/nested/image/manifests/{reference}"


def _badge_url(
    reference: str,
    *,
    owner: str = "example",
    package: str = "nested/image",
) -> str:
    return (
        f"https://ghcr-badge.egpl.dev/{owner}/{package}/size?tag="
        f"{reference.replace(':', '%3A')}"
    )


def test_manifest_inspector_resolves_amd64_and_reuses_pull_token() -> None:
    """Indexes resolve to actual amd64 layers through one repository token."""

    index = json.dumps(
        {
            "schemaVersion": 2,
            "manifests": [
                {
                    "digest": "sha256:arm",
                    "size": 400,
                    "platform": {"os": "linux", "architecture": "arm64"},
                },
                {
                    "digest": "sha256:attestation",
                    "size": 500,
                    "platform": {"os": "unknown", "architecture": "unknown"},
                },
                {
                    "digest": "sha256:amd",
                    "size": 600,
                    "platform": {"os": "linux", "architecture": "amd64"},
                },
            ],
        }
    )
    amd64_manifest = '{"layers":[{"size":10},{"size":25}]}'
    second_manifest = '{"layers":[{"size":40}]}'
    client = _FakeClient(
        {
            _token_url(): ['{"token":"pull-token","expires_in":600}'],
            _manifest_url("Stable"): [index],
            _manifest_url("sha256:amd"): [amd64_manifest],
            _manifest_url("v2"): [second_manifest],
        }
    )
    inspector = GHCRManifestInspector(client, clock=lambda: 100.0)

    first = inspector("ghcr.io/Example/Nested/Image:Stable")
    second = inspector("ghcr.io/example/nested/image:v2")

    assert manifest_size(first).size == 35
    assert manifest_size(second).size == 40
    assert [request.url for request in client.requests] == [
        _token_url(),
        _manifest_url("Stable"),
        _manifest_url("sha256:amd"),
        _manifest_url("v2"),
    ]
    assert client.requests[0].bearer_token is None
    assert all(request.bearer_token == "pull-token" for request in client.requests[1:])


def test_manifest_inspector_prefers_cnab_invocation_over_bundle_config() -> None:
    """A platformless CNAB index resolves to its runnable invocation image."""

    index = json.dumps(
        {
            "schemaVersion": 2,
            "manifests": [
                {
                    "digest": "sha256:config",
                    "annotations": {"io.cnab.manifest.type": "config"},
                },
                {
                    "digest": "sha256:invocation",
                    "annotations": {"io.cnab.manifest.type": "invocation"},
                },
            ],
        }
    )
    invocation = '{"layers":[{"size":20},{"size":22}]}'
    client = _FakeClient(
        {
            _token_url(): ['{"token":"pull-token","expires_in":600}'],
            _manifest_url("v1"): [index],
            _manifest_url("sha256:invocation"): [invocation],
        }
    )

    manifest = GHCRManifestInspector(client)("ghcr.io/example/nested/image:v1")

    assert manifest_size(manifest).size == 42
    assert [request.url for request in client.requests] == [
        _token_url(),
        _manifest_url("v1"),
        _manifest_url("sha256:invocation"),
    ]


def test_manifest_inspector_refreshes_rejected_pull_token() -> None:
    """An expired cached token is replaced once after a registry 401."""

    client = _FakeClient(
        {
            _token_url(): [
                '{"token":"old-token","expires_in":600}',
                '{"token":"new-token","expires_in":600}',
            ],
            _manifest_url("latest"): [
                GitHubResponseError("expired", status_code=401),
                '{"layers":[{"size":12}]}',
            ],
        }
    )

    manifest = GHCRManifestInspector(client, clock=lambda: 100.0)(
        "ghcr.io/example/nested/image:latest"
    )

    assert manifest_size(manifest).size == 12
    assert [request.bearer_token for request in client.requests] == [
        None,
        "old-token",
        None,
        "new-token",
    ]


def test_manifest_inspector_reports_invalid_token_response() -> None:
    """Malformed registry authentication remains a best-effort size failure."""

    diagnostics: list[str] = []
    client = _FakeClient({_token_url(): ["{}"]})

    manifest = GHCRManifestInspector(client, diagnostic=diagnostics.append)(
        "ghcr.io/example/nested/image:latest"
    )

    assert manifest == ""
    assert diagnostics == [
        "GHCR manifest request failed for ghcr.io/example/nested/image:latest: "
        "GHCR token response did not contain a token"
    ]


def test_manifest_inspector_shares_concurrent_missing_child_manifest() -> None:
    """Concurrent historical child-manifest misses share one request."""

    missing_digest = "sha256:missing"
    index = json.dumps(
        {
            "manifests": [
                {
                    "digest": missing_digest,
                    "platform": {"os": "linux", "architecture": "amd64"},
                }
            ]
        }
    )
    diagnostics: list[str] = []
    client = _BlockingMissingClient(
        {
            _token_url(): ['{"token":"pull-token","expires_in":600}'],
            _manifest_url("one"): [index],
            _manifest_url("two"): [index],
        },
        _manifest_url(missing_digest),
        _manifest_url("two"),
    )
    inspector = GHCRManifestInspector(client, diagnostic=diagnostics.append)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(inspector, "ghcr.io/example/nested/image:one")
        assert client.missing_started.wait(timeout=5)
        second = executor.submit(inspector, "ghcr.io/example/nested/image:two")
        assert client.second_root_started.wait(timeout=5)
        client.release_missing.set()

        assert first.result(timeout=5) == ""
        assert second.result(timeout=5) == ""

    assert [request.url for request in client.requests] == [
        _token_url(),
        _manifest_url("one"),
        _manifest_url(missing_digest),
        _manifest_url("two"),
    ]
    assert diagnostics == [
        "GHCR manifest not found for ghcr.io/example/nested/image@sha256:missing"
    ]


def test_badge_size_inspector_uses_exact_reference_and_caches_result() -> None:
    """The final hosted fallback requests and caches one version-specific size."""

    url = _badge_url("sha256:abc")
    client = _FakeClient(
        {url: ["<svg><text>image size</text><text>3.23 MiB</text></svg>"]}
    )
    inspector = GHCRBadgeSizeInspector(client, RequestCircuit())

    first = inspector("Example", "Nested%2FImage", "sha256:abc")
    second = inspector("example", "nested/image", "sha256:abc")

    assert first == 3_386_900
    assert second == first
    assert len(client.requests) == 1
    assert client.requests[0].url == url
    assert client.requests[0].accept == "image/svg+xml"
    assert client.requests[0].policy == GitHubTextRequestPolicy(15.0, 1)


def test_badge_size_inspector_pauses_after_repeated_non_svg_responses() -> None:
    """Hosted outage pages open a circuit instead of delaying every version."""

    client = _FakeClient(
        {
            _badge_url("one"): ["<html><title>Service suspended</title></html>"],
            _badge_url("two"): ["Rate limit exceeded"],
            _badge_url("three"): ["unused"],
        }
    )
    diagnostics: list[str] = []
    circuit = RequestCircuit(
        RequestCircuitSettings(max_concurrent=1, failure_threshold=2),
        clock=lambda: 100.0,
    )
    inspector = GHCRBadgeSizeInspector(
        client,
        circuit,
        diagnostic=diagnostics.append,
    )

    assert inspector("example", "nested/image", "one") == -1
    assert inspector("example", "nested/image", "two") == -1
    assert inspector("example", "nested/image", "three") == -1

    assert [request.url for request in client.requests] == [
        _badge_url("one"),
        _badge_url("two"),
    ]
    assert sum("Pausing GHCR badge size fallback" in line for line in diagnostics) == 1


def test_badge_size_inspector_caches_unsupported_nested_package() -> None:
    """A service capability rejection does not poison outage backpressure."""

    nested_url = _badge_url("one")
    simple_url = _badge_url("latest", package="image")
    client = _FakeClient(
        {
            nested_url: ['{"exception":"InvalidImageError"}'],
            simple_url: ["<svg><text>2 MiB</text></svg>"],
        }
    )
    diagnostics: list[str] = []
    inspector = GHCRBadgeSizeInspector(
        client,
        RequestCircuit(),
        diagnostic=diagnostics.append,
    )

    assert inspector("example", "nested/image", "one") == -1
    assert inspector("example", "nested/image", "two") == -1
    assert inspector("example", "image", "latest") == 2_097_152

    assert [request.url for request in client.requests] == [nested_url, simple_url]
    assert not diagnostics


def test_badge_size_parsing_accepts_service_units_and_rejects_error_badges() -> None:
    """Badge parsing recognizes byte units without interpreting error text."""

    assert parse_size_value("2 MB") == 2_000_000
    assert parse_size_value("2 MiB") == 2_097_152
    assert parse_size_value("8 kb") == 1_000
    assert parse_size_value("512 bytes") == 512
    assert parse_size_value("unknown") == -1
    assert parse_badge_size("<svg><text>1 kB</text><text>2 MB</text></svg>") == (
        2_000_000
    )
    assert parse_badge_size("<svg><text>invalid</text></svg>") == -1
