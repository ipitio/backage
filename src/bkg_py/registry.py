"""GHCR manifest resolution through the OCI Distribution API."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, cast
from urllib.parse import quote, urlencode

from .github import GitHubError, GitHubResponseError

_REGISTRY_PREFIX = "ghcr.io/"
_REGISTRY_URL = "https://ghcr.io"
_AUTH_URL = "https://ghcr.io/token"
_UNAUTHORIZED_STATUS = 401
_DEFAULT_TOKEN_LIFETIME = 300.0
_TOKEN_EXPIRY_MARGIN = 10.0
_MAX_INDEX_DEPTH = 4
_MANIFEST_ACCEPT = ", ".join(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)

DiagnosticSink = Callable[[str], None]


def _ignore_diagnostic(_message: str) -> None:
    pass


class RegistryTextClient(Protocol):  # pylint: disable=too-few-public-methods
    """HTTP behavior needed to retrieve GHCR tokens and manifests."""

    def get_text(
        self,
        url: str,
        *,
        authenticated: bool = False,
        accept: str = "text/html",
        bearer_token: str | None = None,
    ) -> str:
        """Return one text response through a bounded retry policy."""

        raise NotImplementedError


@dataclass(frozen=True)
class _PullToken:
    value: str
    expires_at: float


class GHCRManifestInspector:  # pylint: disable=too-few-public-methods
    """Resolve a GHCR reference to one runnable platform image manifest."""

    def __init__(
        self,
        client: RegistryTextClient,
        *,
        diagnostic: DiagnosticSink = _ignore_diagnostic,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.client = client
        self.diagnostic = diagnostic
        self.clock = clock
        self._tokens: dict[str, _PullToken] = {}
        self._token_lock = threading.Lock()

    def __call__(self, reference: str) -> str:
        """Fetch a single image manifest, resolving indexes by platform."""

        try:
            repository, manifest_ref = _split_reference(reference)
            token = self._pull_token(repository)
            manifest, token = self._manifest(repository, manifest_ref, token)
            for _depth in range(_MAX_INDEX_DEPTH):
                child = _preferred_child_digest(manifest)
                if child is None:
                    return manifest
                manifest, token = self._manifest(repository, child, token)
            self.diagnostic(
                f"GHCR manifest index exceeded {_MAX_INDEX_DEPTH} levels for "
                f"{reference}"
            )
            return ""
        except (GitHubError, json.JSONDecodeError, ValueError) as error:
            self.diagnostic(f"GHCR manifest request failed for {reference}: {error}")
            return ""

    def _pull_token(self, repository: str, *, force: bool = False) -> str:
        with self._token_lock:
            now = self.clock()
            cached = self._tokens.get(repository)
            if (
                not force
                and cached is not None
                and cached.expires_at - _TOKEN_EXPIRY_MARGIN > now
            ):
                return cached.value

            query = urlencode(
                {
                    "service": "ghcr.io",
                    "scope": f"repository:{repository}:pull",
                }
            )
            response = self.client.get_text(
                f"{_AUTH_URL}?{query}",
                accept="application/json",
            )
            value: object = json.loads(response)
            if not isinstance(value, dict):
                raise ValueError("GHCR token response was not an object")
            token_response = cast(dict[str, object], value)
            token = token_response.get("token") or token_response.get("access_token")
            if not isinstance(token, str) or not token:
                raise ValueError("GHCR token response did not contain a token")
            lifetime = _token_lifetime(token_response.get("expires_in"))
            self._tokens[repository] = _PullToken(token, now + lifetime)
            return token

    def _manifest(
        self,
        repository: str,
        reference: str,
        token: str,
    ) -> tuple[str, str]:
        url = _manifest_url(repository, reference)
        try:
            return (
                self.client.get_text(
                    url,
                    accept=_MANIFEST_ACCEPT,
                    bearer_token=token,
                ),
                token,
            )
        except GitHubResponseError as error:
            if error.status_code != _UNAUTHORIZED_STATUS:
                raise
            refreshed = self._pull_token(repository, force=True)
            return (
                self.client.get_text(
                    url,
                    accept=_MANIFEST_ACCEPT,
                    bearer_token=refreshed,
                ),
                refreshed,
            )


def _split_reference(reference: str) -> tuple[str, str]:
    if not reference.casefold().startswith(_REGISTRY_PREFIX):
        raise ValueError("container reference is not hosted on ghcr.io")
    value = reference[len(_REGISTRY_PREFIX) :]
    if "@" in value:
        repository, manifest_ref = value.rsplit("@", 1)
    elif ":" in value:
        repository, manifest_ref = value.rsplit(":", 1)
    else:
        repository, manifest_ref = value, "latest"
    if not repository or not manifest_ref:
        raise ValueError("container reference is incomplete")
    return repository.casefold(), manifest_ref


def _manifest_url(repository: str, reference: str) -> str:
    encoded_repository = quote(repository, safe="/")
    encoded_reference = quote(reference, safe=":._-")
    return f"{_REGISTRY_URL}/v2/{encoded_repository}/manifests/{encoded_reference}"


def _preferred_child_digest(manifest: str) -> str | None:
    value: object = json.loads(manifest)
    if not isinstance(value, dict):
        return None
    manifests = cast(dict[str, object], value).get("manifests")
    if not isinstance(manifests, list):
        return None

    candidates = [
        cast(dict[str, object], item)
        for item in cast(list[object], manifests)
        if isinstance(item, dict)
    ]
    return (
        _first_matching_digest(
            candidates,
            lambda candidate: _platform(candidate) == ("linux", "amd64"),
        )
        or _first_matching_digest(candidates, _has_runnable_platform)
        or _first_matching_digest(
            candidates,
            lambda candidate: _platform(candidate) == ("", ""),
        )
    )


def _first_matching_digest(
    candidates: list[dict[str, object]],
    matches: Callable[[dict[str, object]], bool],
) -> str | None:
    for candidate in candidates:
        if matches(candidate) and (digest := _digest(candidate)):
            return digest
    return None


def _has_runnable_platform(descriptor: dict[str, object]) -> bool:
    os_name, architecture = _platform(descriptor)
    return os_name not in {"", "unknown"} and architecture not in {"", "unknown"}


def _platform(descriptor: dict[str, object]) -> tuple[str, str]:
    value = descriptor.get("platform")
    if not isinstance(value, dict):
        return "", ""
    platform = cast(dict[str, object], value)
    os_name = platform.get("os")
    architecture = platform.get("architecture")
    return (
        os_name.casefold() if isinstance(os_name, str) else "",
        architecture.casefold() if isinstance(architecture, str) else "",
    )


def _digest(descriptor: dict[str, object]) -> str | None:
    value = descriptor.get("digest")
    return value if isinstance(value, str) and value else None


def _token_lifetime(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return _DEFAULT_TOKEN_LIFETIME
    return max(float(value), _TOKEN_EXPIRY_MARGIN * 2)
