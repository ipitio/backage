"""GitHub package-registry archive locators and bounded size adapters."""

from __future__ import annotations

import json
from collections.abc import Callable
from json import JSONDecodeError
from typing import Protocol, cast
from urllib.parse import quote, unquote

from .artifact_sizes import (
    ArtifactSizeRequest,
    ArtifactSizeResult,
    ArtifactSizeSemantics,
    SingleFlightCache,
)
from .enrichment import DeduplicatedDiagnostics, RequestCircuit
from .registry_transport import (
    PackageRegistryError,
    PackageRegistryResource,
    PackageRegistryResponseError,
    PackageRegistrySizeProbe,
    transient_registry_error,
)

DiagnosticSink = Callable[[str], None]
_METADATA_MAX_BYTES = 16 * 1024 * 1024
_METADATA_TIMEOUT_SECONDS = 20.0
_ARCHIVE_TIMEOUT_SECONDS = 20.0
_NPM_HOST = "npm.pkg.github.com"
_NPM_ARCHIVE_HOST = "pkg-npm.githubusercontent.com"
_NUGET_HOST = "nuget.pkg.github.com"
_NUGET_ARCHIVE_HOST = "nugetregistryv2prod.blob.core.windows.net"
_RUBYGEMS_HOST = "rubygems.pkg.github.com"
_RUBYGEMS_ARCHIVE_HOST = "rubygemsregistryv2prod.blob.core.windows.net"


def _ignore_diagnostic(_message: str) -> None:
    pass


class PackageRegistryClient(Protocol):
    """Bounded HTTP operations used by package-registry size adapters."""

    def read_bytes(
        self,
        resource: PackageRegistryResource,
        *,
        max_bytes: int,
    ) -> bytes:
        """Read bounded metadata from explicitly approved registry hosts."""

        raise NotImplementedError

    def probe_size(
        self,
        resource: PackageRegistryResource,
    ) -> PackageRegistrySizeProbe:
        """Probe archive size without consuming the package body."""

        raise NotImplementedError


class _RegistryCircuitUnavailable(RuntimeError):
    """The registry's recovery circuit is currently suppressing requests."""


class _RegistryArtifactSizeAdapter:  # pylint: disable=too-few-public-methods
    """Shared archive probing, backpressure, and concise diagnostics."""

    registry_name = "registry"
    source_name = "github-package-archive"
    registry_host = ""
    archive_hosts: frozenset[str] = frozenset()

    def __init__(
        self,
        client: PackageRegistryClient,
        circuit: RequestCircuit,
        *,
        diagnostic: DiagnosticSink = _ignore_diagnostic,
    ) -> None:
        self.client = client
        self.circuit = circuit
        self.diagnostics = DeduplicatedDiagnostics(diagnostic)

    def resolve(self, request: ArtifactSizeRequest) -> ArtifactSizeResult:
        """Locate and probe one archive while treating enrichment as optional."""

        identity = _request_identity(request)
        try:
            archive_url = self._archive_url(request)
            if archive_url is None:
                return ArtifactSizeResult.unknown(
                    f"{self.registry_name}-metadata-missing"
                )
            probe = self._remote(
                lambda: self.client.probe_size(
                    PackageRegistryResource(
                        archive_url,
                        self.archive_hosts,
                        frozenset({self.registry_host}),
                        total_timeout=_ARCHIVE_TIMEOUT_SECONDS,
                    )
                )
            )
        except _RegistryCircuitUnavailable:
            return ArtifactSizeResult.unknown(f"{self.registry_name}-circuit-open")
        except PackageRegistryError as error:
            self._report_request_error(error, identity)
            return ArtifactSizeResult.unknown(f"{self.registry_name}-request-failed")

        if probe.size < 0:
            reason = probe.reason or "unrecognized-response"
            self.diagnostics.report(
                f"probe:{reason}",
                f"{self.registry_name} artifact sizing could not use the archive "
                f"response ({reason}); leaving size unknown (example: {identity})",
            )
            return ArtifactSizeResult.unknown(f"{self.registry_name}-{reason}")
        return ArtifactSizeResult(
            probe.size,
            ArtifactSizeSemantics.COMPRESSED_DOWNLOAD,
            self.source_name,
        )

    def _archive_url(self, request: ArtifactSizeRequest) -> str | None:
        raise NotImplementedError

    def _metadata_bytes(self, url: str, *, accept: str) -> bytes:
        return self._remote(
            lambda: self.client.read_bytes(
                PackageRegistryResource(
                    url,
                    frozenset({self.registry_host}),
                    frozenset({self.registry_host}),
                    accept=accept,
                    total_timeout=_METADATA_TIMEOUT_SECONDS,
                ),
                max_bytes=_METADATA_MAX_BYTES,
            )
        )

    def _remote[T](self, operation: Callable[[], T]) -> T:
        with self.circuit.request(self.registry_name) as lease:
            if not lease:
                raise _RegistryCircuitUnavailable
            try:
                result = operation()
            except PackageRegistryError as error:
                if transient_registry_error(error):
                    cooldown = lease.record_transient_failure()
                    if cooldown is not None:
                        self.diagnostics.report(
                            f"cooldown:{cooldown}",
                            f"{self.registry_name} artifact-size requests paused for "
                            f"{cooldown:.0f}s after repeated transient failures",
                        )
                else:
                    lease.record_success()
                raise
            lease.record_success()
            return result

    def _metadata_problem(self, key: str, identity: str, detail: str) -> None:
        self.diagnostics.report(
            f"metadata:{key}",
            f"{self.registry_name} artifact metadata {detail}; leaving size unknown "
            f"(example: {identity})",
        )

    def _report_request_error(
        self,
        error: PackageRegistryError,
        identity: str,
    ) -> None:
        if transient_registry_error(error):
            return
        status = (
            error.status_code
            if isinstance(error, PackageRegistryResponseError)
            else None
        )
        category = f"HTTP {status}" if status is not None else type(error).__name__
        self.diagnostics.report(
            f"request:{category}",
            f"{self.registry_name} artifact sizing received {category}; leaving size "
            f"unknown (example: {identity})",
        )


class NpmArtifactSizeAdapter(  # pylint: disable=too-few-public-methods
    _RegistryArtifactSizeAdapter
):
    """Locate npm tarballs through the scoped GitHub packument."""

    registry_name = "npm"
    source_name = "github-npm-archive"
    registry_host = _NPM_HOST
    archive_hosts = frozenset({_NPM_HOST, _NPM_ARCHIVE_HOST})

    def __init__(
        self,
        client: PackageRegistryClient,
        circuit: RequestCircuit,
        *,
        diagnostic: DiagnosticSink = _ignore_diagnostic,
    ) -> None:
        super().__init__(client, circuit, diagnostic=diagnostic)
        self._packuments = SingleFlightCache[tuple[str, str], dict[str, str] | None](
            cache_error=_cache_permanent_registry_error
        )

    def _archive_url(self, request: ArtifactSizeRequest) -> str | None:
        package_name = _npm_package_name(request)
        key = (request.context.owner.casefold(), package_name.casefold())
        packument = self._packuments.get(
            key,
            lambda: self._load_packument(request, package_name),
        )
        return None if packument is None else packument.get(request.version_name)

    def _load_packument(
        self,
        request: ArtifactSizeRequest,
        package_name: str,
    ) -> dict[str, str] | None:
        encoded_name = quote(package_name, safe="@")
        url = f"https://{_NPM_HOST}/{encoded_name}"
        body = self._metadata_bytes(
            url,
            accept="application/vnd.npm.install-v1+json",
        )
        try:
            value: object = json.loads(body)
        except JSONDecodeError, UnicodeDecodeError:
            self._metadata_problem(
                "invalid-json", _request_identity(request), "was not JSON"
            )
            return None
        if not isinstance(value, dict):
            self._metadata_problem(
                "shape", _request_identity(request), "was not an object"
            )
            return None
        versions = cast(dict[str, object], value).get("versions")
        if not isinstance(versions, dict):
            self._metadata_problem(
                "versions-shape",
                _request_identity(request),
                "did not contain a versions object",
            )
            return None

        tarballs: dict[str, str] = {}
        for version, raw_metadata in cast(dict[object, object], versions).items():
            if not isinstance(version, str) or not isinstance(raw_metadata, dict):
                continue
            metadata = cast(dict[str, object], raw_metadata)
            dist = metadata.get("dist")
            if not isinstance(dist, dict):
                continue
            tarball = cast(dict[str, object], dist).get("tarball")
            if isinstance(tarball, str) and tarball:
                tarballs[version] = tarball
        return tarballs


class NuGetArtifactSizeAdapter(  # pylint: disable=too-few-public-methods
    _RegistryArtifactSizeAdapter
):
    """Locate NuGet archives through owner and flat-container metadata."""

    registry_name = "nuget"
    source_name = "github-nuget-archive"
    registry_host = _NUGET_HOST
    archive_hosts = frozenset({_NUGET_HOST, _NUGET_ARCHIVE_HOST})

    def __init__(
        self,
        client: PackageRegistryClient,
        circuit: RequestCircuit,
        *,
        diagnostic: DiagnosticSink = _ignore_diagnostic,
    ) -> None:
        super().__init__(client, circuit, diagnostic=diagnostic)
        self._owner_bases = SingleFlightCache[str, str | None](
            cache_error=_cache_permanent_registry_error
        )
        self._package_versions = SingleFlightCache[
            tuple[str, str], tuple[str, ...] | None
        ](cache_error=_cache_permanent_registry_error)

    def _archive_url(self, request: ArtifactSizeRequest) -> str | None:
        owner = request.context.owner
        package = unquote(request.context.package)
        base = self._owner_bases.get(
            owner.casefold(),
            lambda: self._load_package_base(request, owner),
        )
        if base is None:
            return None
        package_key = package.casefold()
        versions = self._package_versions.get(
            (owner.casefold(), package_key),
            lambda: self._load_package_versions(request, base, package_key),
        )
        if versions is None:
            return None
        requested = request.version_name.casefold().partition("+")[0]
        normalized = next(
            (version for version in versions if version.casefold() == requested),
            None,
        )
        if normalized is None:
            return None
        encoded_package = quote(package_key, safe="")
        encoded_version = quote(normalized, safe="")
        filename = quote(f"{package_key}.{normalized}.nupkg", safe="")
        return f"{base.rstrip('/')}/{encoded_package}/{encoded_version}/{filename}"

    def _load_package_base(
        self,
        request: ArtifactSizeRequest,
        owner: str,
    ) -> str | None:
        url = f"https://{_NUGET_HOST}/{quote(owner, safe='')}/index.json"
        value = self._metadata_json(request, url)
        if not isinstance(value, dict):
            return None
        resources = cast(dict[str, object], value).get("resources")
        if not isinstance(resources, list):
            self._metadata_problem(
                "resources-shape",
                _request_identity(request),
                "did not contain a resources array",
            )
            return None
        for raw_resource in cast(list[object], resources):
            if not isinstance(raw_resource, dict):
                continue
            resource = cast(dict[str, object], raw_resource)
            if not _nuget_resource_type_matches(
                resource.get("@type"),
                "PackageBaseAddress/3.0.0",
            ):
                continue
            resource_url = resource.get("@id")
            if isinstance(resource_url, str) and resource_url:
                return resource_url
        self._metadata_problem(
            "package-base-missing",
            _request_identity(request),
            "did not advertise PackageBaseAddress/3.0.0",
        )
        return None

    def _load_package_versions(
        self,
        request: ArtifactSizeRequest,
        base: str,
        package: str,
    ) -> tuple[str, ...] | None:
        url = f"{base.rstrip('/')}/{quote(package, safe='')}/index.json"
        value = self._metadata_json(request, url)
        if not isinstance(value, dict):
            return None
        raw_versions = cast(dict[str, object], value).get("versions")
        if not isinstance(raw_versions, list):
            self._metadata_problem(
                "flat-versions-shape",
                _request_identity(request),
                "did not contain a flat-container versions array",
            )
            return None
        return tuple(
            version
            for version in cast(list[object], raw_versions)
            if isinstance(version, str) and version
        )

    def _metadata_json(self, request: ArtifactSizeRequest, url: str) -> object | None:
        body = self._metadata_bytes(url, accept="application/json")
        try:
            return json.loads(body)
        except JSONDecodeError, UnicodeDecodeError:
            self._metadata_problem(
                "invalid-json", _request_identity(request), "was not JSON"
            )
            return None


class RubyGemsArtifactSizeAdapter(  # pylint: disable=too-few-public-methods
    _RegistryArtifactSizeAdapter
):
    """Locate gem archives through GitHub's compact package index."""

    registry_name = "rubygems"
    source_name = "github-rubygems-archive"
    registry_host = _RUBYGEMS_HOST
    archive_hosts = frozenset({_RUBYGEMS_HOST, _RUBYGEMS_ARCHIVE_HOST})

    def __init__(
        self,
        client: PackageRegistryClient,
        circuit: RequestCircuit,
        *,
        diagnostic: DiagnosticSink = _ignore_diagnostic,
    ) -> None:
        super().__init__(client, circuit, diagnostic=diagnostic)
        self._package_entries = SingleFlightCache[
            tuple[str, str], tuple[str, ...] | None
        ](cache_error=_cache_permanent_registry_error)

    def _archive_url(self, request: ArtifactSizeRequest) -> str | None:
        owner = request.context.owner
        package = unquote(request.context.package)
        key = (owner.casefold(), package.casefold())
        entries = self._package_entries.get(
            key,
            lambda: self._load_entries(request, owner, package),
        )
        if entries is None:
            return None
        version = request.version_name
        if version in entries:
            archive_version = version
        else:
            platform_entries = tuple(
                entry for entry in entries if entry.startswith(f"{version}-")
            )
            if len(platform_entries) != 1:
                if len(platform_entries) > 1:
                    self._metadata_problem(
                        "ambiguous-platform",
                        _request_identity(request),
                        "listed multiple platform archives for one version",
                    )
                return None
            archive_version = platform_entries[0]
        filename = quote(f"{package}-{archive_version}.gem", safe="")
        return f"https://{_RUBYGEMS_HOST}/{quote(owner, safe='')}/gems/{filename}"

    def _load_entries(
        self,
        request: ArtifactSizeRequest,
        owner: str,
        package: str,
    ) -> tuple[str, ...] | None:
        url = (
            f"https://{_RUBYGEMS_HOST}/{quote(owner, safe='')}/info/"
            f"{quote(package, safe='')}"
        )
        body = self._metadata_bytes(url, accept="text/plain")
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            self._metadata_problem(
                "invalid-text",
                _request_identity(request),
                "was not UTF-8 compact-index text",
            )
            return None
        entries = tuple(
            token
            for line in text.splitlines()
            if line and line != "---"
            if (token := line.partition(" ")[0])
        )
        if not entries:
            self._metadata_problem(
                "empty-info",
                _request_identity(request),
                "did not contain compact-index version entries",
            )
            return None
        return entries


def _npm_package_name(request: ArtifactSizeRequest) -> str:
    package = unquote(request.context.package)
    if package.startswith("@") and "/" in package:
        return package
    return f"@{request.context.owner}/{package}"


def _cache_permanent_registry_error(error: Exception) -> bool:
    return isinstance(error, PackageRegistryError) and not transient_registry_error(
        error
    )


def _nuget_resource_type_matches(value: object, expected: str) -> bool:
    if isinstance(value, str):
        return value == expected
    if isinstance(value, list):
        return expected in value
    return False


def _request_identity(request: ArtifactSizeRequest) -> str:
    return (
        f"{request.context.owner}/{unquote(request.context.package)}/"
        f"{request.version_name}"
    )
