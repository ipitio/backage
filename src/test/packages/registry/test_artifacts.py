"""Tests for package-neutral artifact-size selection."""

from dataclasses import dataclass, field

from bkg_py.packages.registry.artifacts import (
    ArtifactSizeRequest,
    ArtifactSizeResolver,
    ArtifactSizeResult,
    ArtifactSizeSemantics,
    ContainerArtifactSizeAdapter,
)
from bkg_py.packages.versions.metadata import VersionListingContext


def _request(
    *,
    version_id: str = "7",
    stored_size: int = -1,
) -> ArtifactSizeRequest:
    return ArtifactSizeRequest(
        VersionListingContext(
            owner_type="orgs",
            owner="Example",
            repo="Packages",
            package_type="npm",
            package="Demo",
        ),
        version_id,
        "1.2.3",
        stored_size=stored_size,
    )


@dataclass
class _RecordingAdapter:
    requests: list[ArtifactSizeRequest] = field(
        default_factory=list[ArtifactSizeRequest]
    )

    def resolve(self, request: ArtifactSizeRequest) -> ArtifactSizeResult:
        """Record one request and return a stable known value."""

        self.requests.append(request)
        return ArtifactSizeResult(
            42,
            ArtifactSizeSemantics.COMPRESSED_DOWNLOAD,
            "test-metadata",
        )


def test_resolver_reuses_known_immutable_version_size() -> None:
    """A stored immutable version does not repeat external size requests."""

    adapter = _RecordingAdapter()
    result = ArtifactSizeResolver({"npm": adapter}).resolve(_request(stored_size=123))

    assert result.size == 123
    assert result.semantics is ArtifactSizeSemantics.PERSISTED
    assert result.source == "stored-immutable-version"
    assert not adapter.requests


def test_resolver_retries_unknown_and_synthetic_versions() -> None:
    """Unknown values and mutable synthetic versions remain adapter-eligible."""

    adapter = _RecordingAdapter()
    resolver = ArtifactSizeResolver({"npm": adapter})

    unknown = resolver.resolve(_request())
    synthetic = resolver.resolve(_request(version_id="-1", stored_size=123))

    assert unknown.size == 42
    assert synthetic.size == 42
    assert len(adapter.requests) == 2


def test_container_adapter_uses_cumulative_local_size_last() -> None:
    """Docker's local image size is accepted only after preferred sources fail."""

    request = ArtifactSizeRequest(
        VersionListingContext(
            owner_type="orgs",
            owner="Example",
            repo="Demo",
            package_type="container",
            package="Demo",
        ),
        "7",
        "latest",
    )
    references: list[str] = []
    adapter = ContainerArtifactSizeAdapter(
        lambda _reference: "",
        lambda _owner, _package, _reference: -1,
        local_inspector=lambda reference: references.append(reference) or 4_096,
    )

    result = adapter.resolve(request)

    assert result.size == 4_096
    assert result.semantics is ArtifactSizeSemantics.CUMULATIVE_LOCAL
    assert result.source == "docker-image-inspect"
    assert references == ["ghcr.io/example/demo:latest"]


def test_container_adapter_does_not_use_docker_when_manifest_size_is_known() -> None:
    """A preferred compressed manifest total prevents the local fallback."""

    request = ArtifactSizeRequest(
        VersionListingContext(
            owner_type="orgs",
            owner="Example",
            repo="Demo",
            package_type="container",
            package="Demo",
        ),
        "7",
        "latest",
        embedded_metadata='{"layers":[{"size":25}]}',
    )
    adapter = ContainerArtifactSizeAdapter(
        lambda _reference: "",
        lambda _owner, _package, _reference: -1,
        local_inspector=lambda _reference: 1_000,
    )

    result = adapter.resolve(request)

    assert result.size == 25
    assert result.semantics is ArtifactSizeSemantics.COMPRESSED_DOWNLOAD
    assert result.source == "embedded-oci-manifest"
