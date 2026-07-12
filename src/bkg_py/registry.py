"""GHCR container sizing through direct manifests and a hosted fallback."""

from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Protocol, cast
from urllib.parse import quote, unquote, urlencode

from .enrichment import MetricEnrichmentCircuit, transient_enrichment_error
from .github import (
    GitHubError,
    GitHubResponseError,
    GitHubTextRequestPolicy,
)

_REGISTRY_PREFIX = "ghcr.io/"
_REGISTRY_URL = "https://ghcr.io"
_AUTH_URL = "https://ghcr.io/token"
_UNAUTHORIZED_STATUS = 401
_NOT_FOUND_STATUS = 404
_DEFAULT_TOKEN_LIFETIME = 300.0
_TOKEN_EXPIRY_MARGIN = 10.0
_MAX_INDEX_DEPTH = 4
_BADGE_SIZE_URL = "https://ghcr-badge.egpl.dev"
_BADGE_SIZE_SCOPE = "container-size-badge"
_BADGE_SIZE_REQUEST_POLICY = GitHubTextRequestPolicy(
    total_timeout=15.0,
    max_attempts=1,
)
_BADGE_VALUE_PATTERN = re.compile(
    r"<text\b[^>]*>\s*([0-9]+(?:\.[0-9]+)?)\s*([A-Za-z]*)\s*</text>",
    re.IGNORECASE,
)
_SVG_PATTERN = re.compile(r"<svg(?:\s|>)", re.IGNORECASE)
_INVALID_BADGE_PATTERN = re.compile(
    r"<text\b[^>]*>\s*invalid\s*</text>",
    re.IGNORECASE,
)
_BADGE_CAPABILITY_REJECTIONS = frozenset({"InvalidImageError", "InvalidTagError"})
_SIZE_PATTERN = re.compile(r"^([0-9]+(?:\.[0-9]+)?)\s*([A-Za-z]*)$")
_SIZE_UNIT_PATTERN = re.compile(r"^([kKmMgGtTpPeEzZyY])(i?)([Bb])$")
_SIZE_PREFIXES = "KMGTPEZY"
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
    """HTTP behavior needed for GHCR manifests and hosted badge SVGs."""

    def get_text(  # pylint: disable=too-many-arguments
        self,
        url: str,
        *,
        authenticated: bool = False,
        accept: str = "text/html",
        bearer_token: str | None = None,
        policy: GitHubTextRequestPolicy | None = None,
    ) -> str:
        """Return one text response through a bounded retry policy."""

        raise NotImplementedError


@dataclass(frozen=True)
class _PullToken:
    value: str
    expires_at: float


class _ManifestCache:
    def __init__(self) -> None:
        self._requests: dict[tuple[str, str], Future[str]] = {}
        self._lock = threading.Lock()

    def request(self, key: tuple[str, str]) -> tuple[Future[str], bool]:
        """Return a shared request and whether the caller must execute it."""

        with self._lock:
            request = self._requests.get(key)
            if request is not None:
                return request, False
            request = Future[str]()
            self._requests[key] = request
            return request, True

    @staticmethod
    def complete(request: Future[str], manifest: str) -> None:
        """Publish one successful or permanently missing manifest result."""

        request.set_result(manifest)

    def fail(
        self,
        key: tuple[str, str],
        request: Future[str],
        error: Exception,
    ) -> None:
        """Release waiters and allow a later request to retry the lookup."""

        with self._lock:
            if self._requests.get(key) is request:
                del self._requests[key]
        request.set_exception(error)


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
        self._manifest_cache = _ManifestCache()

    def __call__(self, reference: str) -> str:
        """Fetch a single image manifest, resolving indexes by platform."""

        try:
            repository, manifest_ref = _split_reference(reference)
            token = self._pull_token(repository)
            manifest, token = self._manifest(repository, manifest_ref, token)
            if not manifest:
                return ""
            for _depth in range(_MAX_INDEX_DEPTH):
                child = _preferred_child_digest(manifest)
                if child is None:
                    return manifest
                manifest, token = self._manifest(repository, child, token)
                if not manifest:
                    return ""
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
        key = (repository, reference)
        request, execute = self._manifest_cache.request(key)
        if not execute:
            return request.result(), token

        url = _manifest_url(repository, reference)
        current_token = token
        try:
            for attempt in range(2):
                try:
                    manifest = self.client.get_text(
                        url,
                        accept=_MANIFEST_ACCEPT,
                        bearer_token=current_token,
                    )
                except GitHubResponseError as error:
                    if error.status_code == _NOT_FOUND_STATUS:
                        self._manifest_cache.complete(request, "")
                        self._record_missing_manifest(key)
                        return "", current_token
                    if error.status_code != _UNAUTHORIZED_STATUS or attempt > 0:
                        raise
                    current_token = self._pull_token(repository, force=True)
                    continue

                self._manifest_cache.complete(request, manifest)
                return manifest, current_token
            raise AssertionError("manifest authentication loop exhausted")
        except Exception as error:
            self._manifest_cache.fail(key, request, error)
            raise

    def _record_missing_manifest(self, key: tuple[str, str]) -> None:
        repository, reference = key
        self.diagnostic(f"GHCR manifest not found for ghcr.io/{repository}@{reference}")


class GHCRBadgeSizeInspector:  # pylint: disable=too-few-public-methods
    """Retrieve a version-specific public size from the hosted badge service."""

    def __init__(
        self,
        client: RegistryTextClient,
        metric_enrichment: MetricEnrichmentCircuit,
        *,
        diagnostic: DiagnosticSink = _ignore_diagnostic,
    ) -> None:
        self.client = client
        self.metric_enrichment = metric_enrichment
        self.diagnostic = diagnostic
        self._sizes: dict[tuple[str, str, str], int] = {}
        self._unsupported_packages: set[tuple[str, str]] = set()
        self._size_lock = threading.Lock()

    def __call__(self, owner: str, package: str, reference: str) -> int:
        """Return one badge-reported size without stalling on service outages."""

        key = (owner.casefold(), unquote(package).casefold(), reference)
        package_key = key[:2]
        with self._size_lock:
            if package_key in self._unsupported_packages:
                return -1
            cached = self._sizes.get(key)
        if cached is not None:
            return cached

        with self.metric_enrichment.request(_BADGE_SIZE_SCOPE) as enabled:
            if not enabled:
                return -1
            url = _badge_size_url(*key)
            try:
                response = self.client.get_text(
                    url,
                    accept="image/svg+xml",
                    policy=_BADGE_SIZE_REQUEST_POLICY,
                )
            except GitHubError as error:
                self._record_request_failure(url, error)
                if not transient_enrichment_error(error):
                    self._cache(key, -1)
                return -1

            if _SVG_PATTERN.search(response) is None:
                return self._handle_non_svg(response, url, key, package_key)

            self.metric_enrichment.record_success(_BADGE_SIZE_SCOPE)
            size = parse_badge_size(response)
            if size < 0 and _INVALID_BADGE_PATTERN.search(response) is None:
                self.diagnostic(
                    f"GHCR badge size fallback returned an unrecognized SVG for {url}"
                )
            self._cache(key, size)
            return size

    def _handle_non_svg(
        self,
        response: str,
        url: str,
        key: tuple[str, str, str],
        package_key: tuple[str, str],
    ) -> int:
        rejection = _badge_capability_rejection(response)
        if rejection is not None:
            self.metric_enrichment.record_success(_BADGE_SIZE_SCOPE)
            if rejection == "InvalidImageError":
                self._mark_package_unsupported(package_key)
            else:
                self._cache(key, -1)
            return -1

        cooldown = self.metric_enrichment.record_transient_failure(_BADGE_SIZE_SCOPE)
        self.diagnostic(
            f"GHCR badge size fallback returned a non-SVG response for {url}"
        )
        self._report_cooldown(cooldown)
        return -1

    def _record_request_failure(self, url: str, error: GitHubError) -> None:
        cooldown = None
        if transient_enrichment_error(error):
            cooldown = self.metric_enrichment.record_transient_failure(
                _BADGE_SIZE_SCOPE
            )
        else:
            self.metric_enrichment.record_success(_BADGE_SIZE_SCOPE)
        self.diagnostic(f"GHCR badge size fallback failed for {url}: {error}")
        self._report_cooldown(cooldown)

    def _report_cooldown(self, cooldown: float | None) -> None:
        if cooldown is not None:
            self.diagnostic(
                "Pausing GHCR badge size fallback for "
                f"{cooldown:g}s after repeated transient failures"
            )

    def _cache(self, key: tuple[str, str, str], size: int) -> None:
        with self._size_lock:
            self._sizes[key] = size

    def _mark_package_unsupported(self, key: tuple[str, str]) -> None:
        with self._size_lock:
            self._unsupported_packages.add(key)


def parse_size_value(value: str) -> int:
    """Convert a human-readable badge size to bytes."""

    match = _SIZE_PATTERN.fullmatch(value.strip())
    if match is None:
        return -1
    try:
        size = Decimal(match.group(1))
    except InvalidOperation:
        return -1

    unit = match.group(2)
    if unit.casefold() in {"", "b", "byte", "bytes"}:
        return int(size)

    unit_match = _SIZE_UNIT_PATTERN.fullmatch(unit)
    if unit_match is None:
        return -1
    exponent = _SIZE_PREFIXES.find(unit_match.group(1).upper()) + 1
    if exponent <= 0:
        return -1
    if unit_match.group(2):
        multiplier = 1024
    elif unit_match.group(3) == "B":
        multiplier = 1000
    else:
        multiplier = 125
    return int(size * (Decimal(multiplier) ** exponent))


def parse_badge_size(svg: str) -> int:
    """Return the final numeric size advertised by a badge SVG."""

    matches = _BADGE_VALUE_PATTERN.findall(svg)
    for number, unit in reversed(matches):
        size = parse_size_value(f"{number} {unit}")
        if size >= 0:
            return size
    return -1


def _badge_capability_rejection(response: str) -> str | None:
    try:
        value: object = json.loads(response)
    except json.JSONDecodeError:
        return None
    if not isinstance(value, dict):
        return None
    exception = cast(dict[str, object], value).get("exception")
    if isinstance(exception, str) and exception in _BADGE_CAPABILITY_REJECTIONS:
        return exception
    return None


def _badge_size_url(owner: str, package: str, reference: str) -> str:
    owner_path = quote(unquote(owner), safe="")
    package_path = quote(unquote(package), safe="/")
    query = urlencode({"tag": reference})
    return f"{_BADGE_SIZE_URL}/{owner_path}/{package_path}/size?{query}"


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
