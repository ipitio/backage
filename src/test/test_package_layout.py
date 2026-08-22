"""Tests for the owner and package domain APIs."""

from importlib.util import find_spec

from bkg_py.owners import OwnerBatchRequest, OwnerPageAdmissionResult
from bkg_py.owners import batch as owners_batch
from bkg_py.owners import pages as owners_pages
from bkg_py.packages.discovery import PackageListingService
from bkg_py.packages.registry.artifacts import ArtifactSizeResolver
from bkg_py.packages.registry.docker import DockerSizeInspector
from bkg_py.packages.registry.ghcr import GHCRManifestInspector
from bkg_py.packages.registry.graphql import MavenArtifactSizeAdapter
from bkg_py.packages.registry.sizes import NpmArtifactSizeAdapter
from bkg_py.packages.registry.transport import PackageRegistryTransport
from bkg_py.packages.updates import PackageRefreshService
from bkg_py.packages.versions.ingestion import VersionCandidateLoader
from bkg_py.packages.versions.metadata import VersionListingContext
from bkg_py.packages.versions.selection import VersionSelectionSettings
from bkg_py.packages.versions.updates import VersionRefreshService


def test_owner_package_exposes_outer_run_operations() -> None:
    """The owners package provides the outer application-facing API."""

    assert OwnerBatchRequest is owners_batch.OwnerBatchRequest
    assert OwnerPageAdmissionResult is owners_pages.OwnerPageAdmissionResult


def test_package_domain_uses_explicit_versions_and_registry_boundaries() -> None:
    """Package behavior lives in its policy, version, or provider cluster."""

    assert PackageListingService.__module__ == "bkg_py.packages.discovery"
    assert PackageRefreshService.__module__ == "bkg_py.packages.updates"
    assert VersionCandidateLoader.__module__ == "bkg_py.packages.versions.ingestion"
    assert VersionListingContext.__module__ == "bkg_py.packages.versions.metadata"
    assert VersionSelectionSettings.__module__ == "bkg_py.packages.versions.selection"
    assert VersionRefreshService.__module__ == "bkg_py.packages.versions.updates"
    assert ArtifactSizeResolver.__module__ == "bkg_py.packages.registry.artifacts"
    assert DockerSizeInspector.__module__ == "bkg_py.packages.registry.docker"
    assert GHCRManifestInspector.__module__ == "bkg_py.packages.registry.ghcr"
    assert MavenArtifactSizeAdapter.__module__ == "bkg_py.packages.registry.graphql"
    assert NpmArtifactSizeAdapter.__module__ == "bkg_py.packages.registry.sizes"
    assert PackageRegistryTransport.__module__ == "bkg_py.packages.registry.transport"


def test_flat_package_modules_are_not_retained() -> None:
    """The structural cut leaves no compatibility modules at the package root."""

    old_modules = (
        "artifact_sizes",
        "docker_sizes",
        "enrichment",
        "graphql_sizes",
        "package_discovery",
        "package_updates",
        "registry",
        "registry_sizes",
        "registry_transport",
        "version_ingestion",
        "version_selection",
        "version_updates",
        "versions",
    )
    assert all(find_spec(f"bkg_py.{module}") is None for module in old_modules)
