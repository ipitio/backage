"""Tests for direct GHCR manifest resolution."""

from __future__ import annotations

import json
from dataclasses import dataclass

from bkg_py.github import GitHubResponseError
from bkg_py.registry import GHCRManifestInspector
from bkg_py.versions import manifest_size


@dataclass(frozen=True)
class _Request:
    url: str
    accept: str
    bearer_token: str | None


class _FakeClient:
    def __init__(self, responses: dict[str, list[str | Exception]]) -> None:
        self.responses = responses
        self.requests: list[_Request] = []

    def get_text(
        self,
        url: str,
        *,
        authenticated: bool = False,
        accept: str = "text/html",
        bearer_token: str | None = None,
    ) -> str:
        assert not authenticated
        self.requests.append(_Request(url, accept, bearer_token))
        response = self.responses[url].pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _token_url(repository: str = "example/nested/image") -> str:
    scope = repository.replace("/", "%2F")
    return f"https://ghcr.io/token?service=ghcr.io&scope=repository%3A{scope}%3Apull"


def _manifest_url(reference: str) -> str:
    return f"https://ghcr.io/v2/example/nested/image/manifests/{reference}"


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
