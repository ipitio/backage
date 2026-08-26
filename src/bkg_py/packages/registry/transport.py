"""Credential-isolated, body-bounded transport for GitHub package registries."""

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

import httpx

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_MAX_REDIRECTS = 2
_SIZE_CONTENT_RANGE = re.compile(r"^bytes 0-0/([0-9]+)$", re.IGNORECASE)
_OK_STATUS = 200
_PARTIAL_CONTENT_STATUS = 206


class PackageRegistryError(RuntimeError):
    """A package-registry operation could not complete safely."""


class PackageRegistryTransportError(PackageRegistryError):
    """A package-registry host could not be reached within its deadline."""


class PackageRegistryResponseError(PackageRegistryError):
    """A package-registry host returned a non-success response."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class PackageRegistryDecodeError(PackageRegistryError):
    """Package-registry metadata exceeded its safe decoding boundary."""


@dataclass(frozen=True)
class PackageRegistryTransportSettings:
    """Authentication and timeout settings shared with the GitHub client."""

    token: str = field(repr=False)
    user_agent: Callable[[], str]
    connect_timeout: float
    read_timeout: float
    write_timeout: float
    pool_timeout: float


@dataclass(frozen=True)
class PackageRegistryTransportRuntime:
    """Stop and clock hooks shared with the GitHub client."""

    check_stop: Callable[[], None]
    clock: Callable[[], float]


@dataclass(frozen=True)
class PackageRegistryResource:
    """One registry URL with explicit host, credential, and deadline boundaries."""

    url: str
    allowed_hosts: frozenset[str]
    credential_hosts: frozenset[str]
    accept: str = "application/octet-stream"
    total_timeout: float = 20.0


@dataclass(frozen=True)
class PackageRegistrySizeProbe:
    """One bounded archive-size probe result."""

    size: int
    reason: str | None = None


@dataclass(frozen=True)
class _RegistryResponse:
    status_code: int
    headers: httpx.Headers
    body: bytes


class PackageRegistryTransport:
    """Use one pooled client without carrying credentials across host boundaries."""

    def __init__(
        self,
        client: httpx.Client,
        settings: PackageRegistryTransportSettings,
        runtime: PackageRegistryTransportRuntime,
    ) -> None:
        self._client = client
        self._settings = settings
        self._runtime = runtime

    def read_bytes(
        self,
        resource: PackageRegistryResource,
        *,
        max_bytes: int,
    ) -> bytes:
        """Read one bounded registry metadata response from approved hosts."""

        if max_bytes < 1:
            raise ValueError("package registry response limit must be positive")
        response = self._request(resource, max_body_bytes=max_bytes)
        return response.body

    def probe_size(
        self,
        resource: PackageRegistryResource,
    ) -> PackageRegistrySizeProbe:
        """Derive archive bytes from a one-byte request without reading its body."""

        response = self._request(resource, max_body_bytes=0, byte_range=True)
        if response.status_code == _OK_STATUS:
            return PackageRegistrySizeProbe(-1, "range-ignored")
        if response.status_code != _PARTIAL_CONTENT_STATUS:
            return PackageRegistrySizeProbe(-1, "unexpected-success-status")
        content_range = response.headers.get("content-range", "").strip()
        matched = _SIZE_CONTENT_RANGE.fullmatch(content_range)
        if matched is None:
            return PackageRegistrySizeProbe(-1, "invalid-content-range")
        size = int(matched.group(1))
        if size < 1:
            return PackageRegistrySizeProbe(-1, "invalid-content-range")
        return PackageRegistrySizeProbe(size)

    def _request(  # pylint: disable=too-many-locals
        self,
        resource: PackageRegistryResource,
        *,
        max_body_bytes: int,
        byte_range: bool = False,
    ) -> _RegistryResponse:
        if resource.total_timeout <= 0:
            raise ValueError("package registry timeout must be positive")
        allowed_hosts = frozenset(host.casefold() for host in resource.allowed_hosts)
        credential_hosts = frozenset(
            host.casefold() for host in resource.credential_hosts
        )
        if not allowed_hosts:
            raise ValueError("package registry host allowlist cannot be empty")
        if not credential_hosts <= allowed_hosts:
            raise ValueError("package registry credential hosts must be allowed")

        deadline = self._runtime.clock() + resource.total_timeout
        current_url = resource.url
        for redirect_count in range(_MAX_REDIRECTS + 1):
            host = _validated_host(current_url, allowed_hosts)
            self._runtime.check_stop()
            headers = self._headers(
                host,
                credential_hosts,
                resource.accept,
                byte_range=byte_range,
            )
            try:
                with self._client.stream(
                    "GET",
                    current_url,
                    headers=headers,
                    timeout=self._timeout(self._remaining(deadline)),
                    follow_redirects=False,
                ) as response:
                    if response.status_code in _REDIRECT_STATUSES:
                        current_url = self._redirect_url(
                            response,
                            current_url,
                            allowed_hosts,
                            redirect_count,
                        )
                        continue
                    self._raise_for_status(response)
                    return _RegistryResponse(
                        response.status_code,
                        response.headers,
                        self._bounded_body(response, max_body_bytes),
                    )
            except httpx.TransportError as error:
                operation = _display_url(current_url)
                raise PackageRegistryTransportError(
                    self._redact(
                        f"GitHub package registry transport failed for {operation}"
                    )
                ) from error

        raise AssertionError("package registry redirect loop exhausted")

    def _redirect_url(
        self,
        response: httpx.Response,
        current_url: str,
        allowed_hosts: frozenset[str],
        redirect_count: int,
    ) -> str:
        location = response.headers.get("location")
        if not location:
            raise PackageRegistryResponseError(
                "GitHub package registry returned a redirect without a location",
                status_code=response.status_code,
            )
        if redirect_count >= _MAX_REDIRECTS:
            raise PackageRegistryResponseError(
                "GitHub package registry exceeded its redirect limit",
                status_code=response.status_code,
            )
        redirected = urljoin(current_url, location)
        _validated_host(redirected, allowed_hosts)
        return redirected

    def _headers(
        self,
        host: str,
        credential_hosts: frozenset[str],
        accept: str,
        *,
        byte_range: bool,
    ) -> dict[str, str]:
        headers = {
            "Accept": accept,
            "User-Agent": self._settings.user_agent(),
        }
        if host in credential_hosts and self._settings.token:
            headers["Authorization"] = f"Bearer {self._settings.token}"
        if byte_range:
            headers["Range"] = "bytes=0-0"
        return headers

    def _bounded_body(self, response: httpx.Response, max_body_bytes: int) -> bytes:
        if max_body_bytes == 0:
            return b""
        body = bytearray()
        for chunk in response.iter_bytes():
            self._runtime.check_stop()
            if len(body) + len(chunk) > max_body_bytes:
                raise PackageRegistryDecodeError(
                    "GitHub package registry metadata exceeded its byte limit"
                )
            body.extend(chunk)
        return bytes(body)

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if response.is_success:
            return
        url = _display_url(str(response.request.url))
        raise PackageRegistryResponseError(
            f"GitHub returned HTTP {response.status_code} for "
            f"{response.request.method} {url}",
            status_code=response.status_code,
        )

    def _timeout(self, remaining: float) -> httpx.Timeout:
        return httpx.Timeout(
            connect=min(self._settings.connect_timeout, remaining),
            read=min(self._settings.read_timeout, remaining),
            write=min(self._settings.write_timeout, remaining),
            pool=min(self._settings.pool_timeout, remaining),
        )

    def _remaining(self, deadline: float) -> float:
        remaining = deadline - self._runtime.clock()
        if remaining <= 0:
            raise PackageRegistryTransportError(
                "GitHub package registry operation exceeded its total timeout"
            )
        return remaining

    def _redact(self, value: str) -> str:
        if not self._settings.token:
            return value
        return value.replace(self._settings.token, "[REDACTED]")


def transient_registry_error(error: PackageRegistryError) -> bool:
    """Return whether a package-registry request may recover after cooldown."""

    return isinstance(error, PackageRegistryTransportError) or (
        isinstance(error, PackageRegistryResponseError)
        and error.status_code in {429, 500, 502, 503, 504}
    )


def _validated_host(url: str, allowed_hosts: frozenset[str]) -> str:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as error:
        raise PackageRegistryError(
            "GitHub package registry returned an invalid URL"
        ) from error
    host = (parsed.hostname or "").casefold()
    approved_origin = (
        parsed.scheme.casefold() == "https"
        and bool(host)
        and host in allowed_hosts
        and port in (None, 443)
    )
    has_no_userinfo = parsed.username is None and parsed.password is None
    if not approved_origin or not has_no_userinfo:
        raise PackageRegistryError(
            f"GitHub package registry URL is not allowed: {_display_url(url)}"
        )
    return host


def _display_url(url: str) -> str:
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or "unknown-host"
        port = parsed.port
    except ValueError:
        return "invalid-url"
    authority = f"{host}:{port}" if port is not None else host
    return f"{parsed.scheme or 'unknown'}://{authority}{parsed.path or '/'}"
