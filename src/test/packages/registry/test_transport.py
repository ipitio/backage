"""Tests for credential-isolated GitHub package-registry transport."""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest

from bkg_py.packages.registry.transport import (
    PackageRegistryDecodeError,
    PackageRegistryError,
    PackageRegistryResource,
    PackageRegistryTransport,
    PackageRegistryTransportRuntime,
    PackageRegistryTransportSettings,
)

TEST_TOKEN = "github_pat_secret"


class _UnreadableStream(httpx.SyncByteStream):
    def __iter__(self) -> Iterator[bytes]:
        raise AssertionError("range-ignoring response body was consumed")


def _transport(handler: httpx.MockTransport) -> PackageRegistryTransport:
    return PackageRegistryTransport(
        httpx.Client(transport=handler),
        PackageRegistryTransportSettings(
            token=TEST_TOKEN,
            user_agent=lambda: "test-agent",
            connect_timeout=10,
            read_timeout=60,
            write_timeout=60,
            pool_timeout=10,
        ),
        PackageRegistryTransportRuntime(
            check_stop=lambda: None,
            clock=lambda: 0,
        ),
    )


def test_metadata_is_authenticated_and_bounded() -> None:
    """Registry metadata uses its designated host and a strict response limit."""

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://npm.pkg.github.com/@example%2Fdemo"
        assert request.headers["authorization"] == f"Bearer {TEST_TOKEN}"
        assert request.headers["accept"] == "application/json"
        assert request.headers["user-agent"] == "test-agent"
        return httpx.Response(200, content=b"1234")

    transport = _transport(httpx.MockTransport(respond))
    resource = PackageRegistryResource(
        "https://npm.pkg.github.com/@example%2Fdemo",
        frozenset({"npm.pkg.github.com"}),
        frozenset({"npm.pkg.github.com"}),
        accept="application/json",
    )

    assert transport.read_bytes(resource, max_bytes=4) == b"1234"
    with pytest.raises(PackageRegistryDecodeError, match="byte limit"):
        transport.read_bytes(resource, max_bytes=3)


def test_size_strips_credentials_from_approved_redirect() -> None:
    """A signed storage redirect retains the range but never the GitHub token."""

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.headers["range"] == "bytes=0-0"
        if request.url.host == "npm.pkg.github.com":
            assert request.headers["authorization"] == f"Bearer {TEST_TOKEN}"
            return httpx.Response(
                302,
                headers={
                    "location": "https://pkg-npm.githubusercontent.com/blob?sig=value"
                },
            )
        assert request.url.host == "pkg-npm.githubusercontent.com"
        assert "authorization" not in request.headers
        return httpx.Response(206, headers={"content-range": "bytes 0-0/1403"})

    transport = _transport(httpx.MockTransport(respond))
    resource = PackageRegistryResource(
        "https://npm.pkg.github.com/download/example",
        frozenset({"npm.pkg.github.com", "pkg-npm.githubusercontent.com"}),
        frozenset({"npm.pkg.github.com"}),
    )

    probe = transport.probe_size(resource)

    assert probe.size == 1403
    assert probe.reason is None


def test_rejects_unapproved_redirect_before_requesting_it() -> None:
    """Metadata cannot redirect a credentialed request to an arbitrary host."""

    requests: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.host)
        return httpx.Response(
            302,
            headers={"location": "https://packages.example/archive"},
        )

    transport = _transport(httpx.MockTransport(respond))
    resource = PackageRegistryResource(
        "https://npm.pkg.github.com/download/example",
        frozenset({"npm.pkg.github.com"}),
        frozenset({"npm.pkg.github.com"}),
    )

    with pytest.raises(PackageRegistryError, match="URL is not allowed"):
        transport.probe_size(resource)
    assert requests == ["npm.pkg.github.com"]


def test_range_ignoring_response_is_not_consumed() -> None:
    """A server returning 200 cannot make a size probe download its archive."""

    transport = _transport(
        httpx.MockTransport(
            lambda _request: httpx.Response(200, stream=_UnreadableStream())
        )
    )
    resource = PackageRegistryResource(
        "https://npm.pkg.github.com/download/example",
        frozenset({"npm.pkg.github.com"}),
        frozenset({"npm.pkg.github.com"}),
    )

    probe = transport.probe_size(resource)

    assert probe.size == -1
    assert probe.reason == "range-ignored"
