"""Structural tests for the discovery domain package."""

from __future__ import annotations

import ast
from importlib.util import find_spec
from pathlib import Path

import bkg_py.discovery
from bkg_py.discovery import (
    DiscoveryError,
    OwnerIdentity,
    OwnerIdentityCache,
    OwnerIdentityResolver,
    OwnerLookupResult,
)
from bkg_py.discovery.values import normalize_owner_lines

from ..layout_support import forbidden_imports, package_path

_FORBIDDEN_DEPENDENCIES = frozenset(
    {
        "application",
        "database",
        "owners",
        "packages",
        "publication",
        "run",
        "workspace",
    }
)


def test_discovery_root_exposes_only_identity_primitives() -> None:
    """The package root remains a small cross-domain identity API."""

    assert DiscoveryError.__module__ == "bkg_py.discovery.authenticated"
    assert OwnerIdentity.__module__ == "bkg_py.discovery.authenticated"
    assert OwnerIdentityCache.__module__ == "bkg_py.discovery.authenticated"
    assert OwnerIdentityResolver.__module__ == "bkg_py.discovery.authenticated"
    assert OwnerLookupResult.__module__ == "bkg_py.discovery.authenticated"
    assert normalize_owner_lines.__module__ == "bkg_py.discovery.values"
    assert set(bkg_py.discovery.__all__) == {
        "DiscoveryError",
        "OwnerIdentity",
        "OwnerIdentityCache",
        "OwnerIdentityResolver",
        "OwnerLookupResult",
    }


def test_discovery_is_a_package_without_flat_companion_modules() -> None:
    """The structural move leaves no flat compatibility implementation."""

    package = package_path("bkg_py.discovery")
    specification = find_spec("bkg_py.discovery")
    assert specification is not None
    assert specification.submodule_search_locations is not None
    assert package.name == "discovery"
    assert not package.with_suffix(".py").exists()
    assert find_spec("bkg_py.discovery_fallback") is None
    assert find_spec("bkg_py.discovery_operations") is None


def test_discovery_does_not_depend_on_composed_domains() -> None:
    """Traversal and phase policy remain below composition and owner admission."""

    dependencies: list[tuple[Path, str]] = []
    for source in package_path("bkg_py.discovery").glob("*.py"):
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        dependencies.extend(
            (source, dependency)
            for node in ast.walk(tree)
            if isinstance(node, ast.Import | ast.ImportFrom)
            for dependency in forbidden_imports(node, _FORBIDDEN_DEPENDENCIES)
        )

    assert not dependencies
