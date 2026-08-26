"""Tests for non-container GitHub package archive sizing."""

import json

from bkg_py.packages.enrichment import RequestCircuit, RequestCircuitSettings
from bkg_py.packages.registry.artifacts import ArtifactSizeRequest
from bkg_py.packages.registry.sizes import (
    NpmArtifactSizeAdapter,
    NuGetArtifactSizeAdapter,
    RubyGemsArtifactSizeAdapter,
)
from bkg_py.packages.registry.transport import (
    PackageRegistryResource,
    PackageRegistryResponseError,
    PackageRegistrySizeProbe,
    PackageRegistryTransportError,
)
from bkg_py.packages.versions.metadata import VersionListingContext


class _FakeRegistryClient:
    def __init__(
        self,
        metadata: dict[str, bytes | Exception] | None = None,
        sizes: dict[str, PackageRegistrySizeProbe | Exception] | None = None,
    ) -> None:
        self.metadata = metadata or {}
        self.sizes = sizes or {}
        self.metadata_requests: list[PackageRegistryResource] = []
        self.size_requests: list[PackageRegistryResource] = []

    def read_bytes(
        self,
        resource: PackageRegistryResource,
        *,
        max_bytes: int,
    ) -> bytes:
        """Return configured metadata and record its resource boundary."""

        self.metadata_requests.append(resource)
        value = self.metadata[resource.url]
        if isinstance(value, Exception):
            raise value
        assert len(value) <= max_bytes
        return value

    def probe_size(
        self,
        resource: PackageRegistryResource,
    ) -> PackageRegistrySizeProbe:
        """Return a configured archive probe and record its resource boundary."""

        self.size_requests.append(resource)
        value = self.sizes[resource.url]
        if isinstance(value, Exception):
            raise value
        return value


def _request(
    package_type: str,
    *,
    owner: str,
    package: str,
    version_id: str,
    version_name: str,
) -> ArtifactSizeRequest:
    return ArtifactSizeRequest(
        VersionListingContext(
            owner_type="orgs",
            owner=owner,
            repo="Packages",
            package_type=package_type,
            package=package,
        ),
        version_id,
        version_name,
    )


def test_npm_adapter_caches_packument_and_probes_exact_tarballs() -> None:
    """Concurrent-version metadata is package-scoped while sizes stay exact."""

    packument_url = "https://npm.pkg.github.com/@github%2Fprettier-config"
    first_url = "https://npm.pkg.github.com/download/first"
    second_url = "https://npm.pkg.github.com/download/second"
    client = _FakeRegistryClient(
        {
            packument_url: json.dumps(
                {
                    "versions": {
                        "0.0.5": {"dist": {"tarball": first_url}},
                        "0.0.6": {"dist": {"tarball": second_url}},
                    }
                }
            ).encode()
        },
        {
            first_url: PackageRegistrySizeProbe(1200),
            second_url: PackageRegistrySizeProbe(1403),
        },
    )
    adapter = NpmArtifactSizeAdapter(client, RequestCircuit())

    first = adapter.resolve(
        _request(
            "npm",
            owner="github",
            package="prettier-config",
            version_id="1",
            version_name="0.0.5",
        )
    )
    second = adapter.resolve(
        _request(
            "npm",
            owner="github",
            package="prettier-config",
            version_id="2",
            version_name="0.0.6",
        )
    )

    assert first.size == 1200
    assert second.size == 1403
    assert len(client.metadata_requests) == 1
    assert [request.url for request in client.size_requests] == [first_url, second_url]
    assert client.size_requests[0].credential_hosts == frozenset({"npm.pkg.github.com"})
    assert "pkg-npm.githubusercontent.com" in client.size_requests[0].allowed_hosts


def test_nuget_adapter_uses_service_index_and_normalized_version() -> None:
    """NuGet size lookup follows its advertised flat-container base."""

    service_url = "https://nuget.pkg.github.com/DbUp/index.json"
    base = "https://nuget.pkg.github.com/DbUp/download"
    versions_url = f"{base}/dbup-core/index.json"
    archive_url = f"{base}/dbup-core/6.1.1/dbup-core.6.1.1.nupkg"
    client = _FakeRegistryClient(
        {
            service_url: json.dumps(
                {"resources": [{"@id": base, "@type": "PackageBaseAddress/3.0.0"}]}
            ).encode(),
            versions_url: json.dumps({"versions": ["6.1.1"]}).encode(),
        },
        {archive_url: PackageRegistrySizeProbe(92_737)},
    )
    adapter = NuGetArtifactSizeAdapter(client, RequestCircuit())

    result = adapter.resolve(
        _request(
            "nuget",
            owner="DbUp",
            package="DbUp-Core",
            version_id="7",
            version_name="6.1.1+build.4",
        )
    )

    assert result.size == 92_737
    assert [request.url for request in client.metadata_requests] == [
        service_url,
        versions_url,
    ]
    assert client.size_requests[0].url == archive_url
    assert "nugetregistryv2prod.blob.core.windows.net" in (
        client.size_requests[0].allowed_hosts
    )


def test_rubygems_adapter_prefers_default_and_rejects_ambiguous_platforms() -> None:
    """Compact-index entries locate one archive without choosing among platforms."""

    info_url = "https://rubygems.pkg.github.com/github/info/example-gem"
    archive_url = "https://rubygems.pkg.github.com/github/gems/example-gem-1.0.0.gem"
    client = _FakeRegistryClient(
        {
            info_url: (
                b"---\n1.0.0 |checksum:one\n"
                b"2.0.0-x86_64-linux |checksum:two\n"
                b"2.0.0-arm64-darwin |checksum:three\n"
            )
        },
        {archive_url: PackageRegistrySizeProbe(4800)},
    )
    diagnostics: list[str] = []
    adapter = RubyGemsArtifactSizeAdapter(
        client,
        RequestCircuit(),
        diagnostic=diagnostics.append,
    )

    default = adapter.resolve(
        _request(
            "rubygems",
            owner="github",
            package="example-gem",
            version_id="8",
            version_name="1.0.0",
        )
    )
    ambiguous = adapter.resolve(
        _request(
            "rubygems",
            owner="github",
            package="example-gem",
            version_id="9",
            version_name="2.0.0",
        )
    )

    assert default.size == 4800
    assert ambiguous.size == -1
    assert len(client.metadata_requests) == 1
    assert len(client.size_requests) == 1
    assert "multiple platform archives" in diagnostics[0]


def test_malformed_metadata_is_cached_and_diagnosed_once() -> None:
    """One malformed packument does not fan out across selected versions."""

    packument_url = "https://npm.pkg.github.com/@example%2Fdemo"
    client = _FakeRegistryClient({packument_url: b"[]"})
    diagnostics: list[str] = []
    adapter = NpmArtifactSizeAdapter(
        client,
        RequestCircuit(),
        diagnostic=diagnostics.append,
    )

    for version_id in ("1", "2"):
        result = adapter.resolve(
            _request(
                "npm",
                owner="example",
                package="demo",
                version_id=version_id,
                version_name=f"1.0.{version_id}",
            )
        )
        assert result.size == -1

    assert len(client.metadata_requests) == 1
    assert len(diagnostics) == 1
    assert "was not an object" in diagnostics[0]


def test_permanent_metadata_failure_is_cached_for_the_process() -> None:
    """One missing packument is not requested again for every selected version."""

    packument_url = "https://npm.pkg.github.com/@example%2Fdemo"
    client = _FakeRegistryClient(
        {
            packument_url: PackageRegistryResponseError(
                "missing package",
                status_code=404,
            )
        }
    )
    diagnostics: list[str] = []
    adapter = NpmArtifactSizeAdapter(
        client,
        RequestCircuit(),
        diagnostic=diagnostics.append,
    )

    for version_id in ("1", "2"):
        result = adapter.resolve(
            _request(
                "npm",
                owner="example",
                package="demo",
                version_id=version_id,
                version_name=f"1.0.{version_id}",
            )
        )
        assert result.size == -1

    assert len(client.metadata_requests) == 1
    assert len(diagnostics) == 1
    assert "HTTP 404" in diagnostics[0]


def test_transient_registry_failures_open_one_adapter_circuit() -> None:
    """Repeated metadata failures pause only the affected package registry."""

    packument_url = "https://npm.pkg.github.com/@example%2Fdemo"
    client = _FakeRegistryClient(
        {packument_url: PackageRegistryTransportError("temporary registry outage")}
    )
    diagnostics: list[str] = []
    adapter = NpmArtifactSizeAdapter(
        client,
        RequestCircuit(
            RequestCircuitSettings(
                failure_threshold=2,
                cooldown_seconds=300,
            )
        ),
        diagnostic=diagnostics.append,
    )

    for version_id in ("1", "2", "3"):
        result = adapter.resolve(
            _request(
                "npm",
                owner="example",
                package="demo",
                version_id=version_id,
                version_name=f"1.0.{version_id}",
            )
        )
        assert result.size == -1

    assert len(client.metadata_requests) == 2
    assert diagnostics == [
        "npm artifact-size requests paused for 300s after repeated transient failures"
    ]
