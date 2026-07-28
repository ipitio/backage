"""Package-neutral artifact-size resolution and container adapters."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from concurrent.futures import Future
from dataclasses import dataclass
from enum import StrEnum
from threading import Lock
from typing import Protocol
from urllib.parse import unquote

from .versions import ManifestSizeResult, VersionListingContext, manifest_size

DiagnosticSink = Callable[[str], None]
ManifestInspector = Callable[[str], str]
HostedContainerSizeInspector = Callable[[str, str, str], int]
LocalContainerSizeInspector = Callable[[str], int]


def _ignore_diagnostic(_message: str) -> None:
    pass


def _no_local_container_size(_reference: str) -> int:
    return -1


def _discard_cached_error(_error: Exception) -> bool:
    return False


class ArtifactSizeSemantics(StrEnum):
    """Meaning of a resolved byte value before it enters the shared size field."""

    UNKNOWN = "unknown"
    PERSISTED = "persisted"
    COMPRESSED_DOWNLOAD = "compressed-download"
    CUMULATIVE_LOCAL = "cumulative-local"


@dataclass(frozen=True)
class ArtifactSizeResult:
    """Best available byte value and its in-memory derivation."""

    size: int
    semantics: ArtifactSizeSemantics
    source: str

    def __post_init__(self) -> None:
        if self.size < -1:
            raise ValueError("artifact size cannot be less than -1")
        if self.size < 0 and self.semantics is not ArtifactSizeSemantics.UNKNOWN:
            raise ValueError("unknown artifact size requires unknown semantics")
        if self.size >= 0 and self.semantics is ArtifactSizeSemantics.UNKNOWN:
            raise ValueError("known artifact size requires known semantics")
        if not self.source:
            raise ValueError("artifact size source cannot be empty")

    @property
    def known(self) -> bool:
        """Return whether a nonnegative byte value was resolved."""

        return self.size >= 0

    @classmethod
    def unknown(cls, source: str = "unavailable") -> ArtifactSizeResult:
        """Return an unresolved result with a concise internal source."""

        return cls(-1, ArtifactSizeSemantics.UNKNOWN, source)


@dataclass(frozen=True)
class ArtifactSizeRequest:
    """Package version inputs shared by every size adapter."""

    context: VersionListingContext
    version_id: str
    version_name: str
    embedded_metadata: str = ""
    stored_size: int = -1
    allow_hosted_fallback: bool = False


class ArtifactSizeAdapter(Protocol):  # pylint: disable=too-few-public-methods
    """Resolve a size for one package type without changing persistence."""

    def resolve(self, request: ArtifactSizeRequest) -> ArtifactSizeResult:
        """Return a resolved or explicitly unknown artifact size."""

        raise NotImplementedError


class SingleFlightCache[K, V]:  # pylint: disable=too-few-public-methods
    """Share one metadata request between concurrent artifact-size workers."""

    def __init__(
        self,
        *,
        cache_error: Callable[[Exception], bool] = _discard_cached_error,
    ) -> None:
        self._requests: dict[K, Future[V]] = {}
        self._lock = Lock()
        self._cache_error = cache_error

    def get(self, key: K, loader: Callable[[], V]) -> V:
        """Load one value or wait for the worker already loading the same key."""

        with self._lock:
            request = self._requests.get(key)
            execute = request is None
            if request is None:
                request = Future[V]()
                self._requests[key] = request
        if not execute:
            return request.result()

        try:
            value = loader()
        except Exception as error:
            if not self._cache_error(error):
                with self._lock:
                    if self._requests.get(key) is request:
                        del self._requests[key]
            request.set_exception(error)
            raise
        request.set_result(value)
        return value


class ArtifactSizeResolver:  # pylint: disable=too-few-public-methods
    """Select a size adapter by package type and reuse immutable stored values."""

    def __init__(
        self, adapters: Mapping[str, ArtifactSizeAdapter] | None = None
    ) -> None:
        self._adapters = {
            package_type.casefold(): adapter
            for package_type, adapter in (adapters or {}).items()
        }

    def resolve(self, request: ArtifactSizeRequest) -> ArtifactSizeResult:
        """Return the best value without re-fetching known immutable versions."""

        if request.version_id != "-1" and request.stored_size >= 0:
            return ArtifactSizeResult(
                request.stored_size,
                ArtifactSizeSemantics.PERSISTED,
                "stored-immutable-version",
            )
        adapter = self._adapters.get(request.context.package_type.casefold())
        if adapter is None:
            return ArtifactSizeResult.unknown("unsupported-package-type")
        return adapter.resolve(request)


@dataclass(frozen=True)
class ContainerArtifactSizeAdapter:
    """Resolve GHCR compressed payload size through bounded fallback sources."""

    manifest_inspector: ManifestInspector
    hosted_inspector: HostedContainerSizeInspector
    diagnostic: DiagnosticSink = _ignore_diagnostic
    local_inspector: LocalContainerSizeInspector = _no_local_container_size

    def resolve(self, request: ArtifactSizeRequest) -> ArtifactSizeResult:
        """Try embedded, registry, allowed hosted, then local container sizing."""

        embedded = manifest_size(request.embedded_metadata)
        self._report_manifest_fallback(
            embedded,
            f"{request.context.owner}/{request.context.package}/"
            f"{request.version_id} embedded manifest",
        )
        if embedded.known:
            return ArtifactSizeResult(
                embedded.size,
                ArtifactSizeSemantics.COMPRESSED_DOWNLOAD,
                "embedded-oci-manifest",
            )

        reference = self._manifest_reference(request)
        inspected = manifest_size(self.manifest_inspector(reference))
        self._report_manifest_fallback(inspected, f"{reference} inspected manifest")
        if inspected.known:
            return ArtifactSizeResult(
                inspected.size,
                ArtifactSizeSemantics.COMPRESSED_DOWNLOAD,
                "ghcr-manifest",
            )

        if request.allow_hosted_fallback:
            hosted_size = self.hosted_inspector(
                request.context.owner,
                request.context.package,
                request.version_name,
            )
            if hosted_size >= 0:
                return ArtifactSizeResult(
                    hosted_size,
                    ArtifactSizeSemantics.COMPRESSED_DOWNLOAD,
                    "ghcr-badge",
                )
        local_size = self.local_inspector(reference)
        if local_size >= 0:
            return ArtifactSizeResult(
                local_size,
                ArtifactSizeSemantics.CUMULATIVE_LOCAL,
                "docker-image-inspect",
            )
        return ArtifactSizeResult.unknown("container-size-unavailable")

    @staticmethod
    def _manifest_reference(request: ArtifactSizeRequest) -> str:
        owner = request.context.owner.lower()
        package = unquote(request.context.package).lower()
        separator = "@" if request.version_name.startswith("sha256:") else ":"
        return f"ghcr.io/{owner}/{package}{separator}{request.version_name}"

    def _report_manifest_fallback(
        self,
        result: ManifestSizeResult,
        context: str,
    ) -> None:
        fallback_reason = result.fallback_reason
        if fallback_reason is None:
            return
        summary = result.diagnostic_summary
        suffix = f"; {summary}" if summary else ""
        self.diagnostic(
            f"Unable to derive container size from {context}: {fallback_reason}{suffix}"
        )
