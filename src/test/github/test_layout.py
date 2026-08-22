"""Structural tests for the GitHub client package boundary."""

from __future__ import annotations

import ast
from importlib.util import find_spec
from pathlib import Path

import bkg_py.github
from bkg_py.github import (
    GitHubClient,
    GitHubError,
    GitHubRateAccounting,
    GitHubSettings,
)


def _github_package() -> Path:
    specification = find_spec("bkg_py.github")
    assert specification is not None
    assert specification.origin is not None
    return Path(specification.origin).parent


def test_github_root_exposes_only_the_supported_client_surface() -> None:
    """The package root remains a deliberate cross-domain API."""

    assert GitHubClient.__module__ == "bkg_py.github.client"
    assert GitHubError.__module__ == "bkg_py.github.errors"
    assert GitHubRateAccounting.__module__ == "bkg_py.github.rate"
    assert GitHubSettings.__module__ == "bkg_py.github.settings"
    assert set(bkg_py.github.__all__) == {
        "GitHubClient",
        "GitHubDecodeError",
        "GitHubError",
        "GitHubGraphQLError",
        "GitHubJsonResponse",
        "GitHubNotFoundError",
        "GitHubRateAccounting",
        "GitHubResponseError",
        "GitHubRuntime",
        "GitHubSettings",
        "GitHubTextRequestPolicy",
        "GitHubTransportError",
    }


def test_github_is_a_package_without_the_former_flat_module() -> None:
    """The structural move leaves no flat compatibility implementation."""

    package = _github_package()
    specification = find_spec("bkg_py.github")
    assert specification is not None
    assert specification.submodule_search_locations is not None
    assert package.name == "github"
    assert not package.with_suffix(".py").exists()


def test_github_package_does_not_depend_on_package_implementations() -> None:
    """Registry behavior is injected without a GitHub-to-package import edge."""

    package = _github_package()
    dependencies: list[tuple[Path, str]] = []
    for source in package.glob("*.py"):
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                dependencies.extend(
                    (source, alias.name)
                    for alias in node.names
                    if alias.name == "bkg_py.packages"
                    or alias.name.startswith("bkg_py.packages.")
                )
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if (
                    module == "bkg_py.packages"
                    or module.startswith("bkg_py.packages.")
                    or (node.level >= 2 and module.split(".", 1)[0] == "packages")
                ):
                    dependencies.append((source, module))

    assert not dependencies
