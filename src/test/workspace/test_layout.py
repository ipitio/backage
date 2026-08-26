"""Structural tests for workspace Git capability boundaries."""

from importlib.util import find_spec
from pathlib import Path

import bkg_py.workspace
from bkg_py.workspace import GitIndexRepository, GitSourceRepository
from bkg_py.workspace.handoff import GitControlRefRepository
from bkg_py.workspace.merge_configuration import GitLocalConfiguration
from bkg_py.workspace.publication import GitBranchPublisher


def test_workspace_root_exposes_capability_specific_repositories() -> None:
    """Callers choose source or index behavior instead of a broad Git adapter."""

    assert GitSourceRepository.__module__ == "bkg_py.workspace.source"
    assert GitIndexRepository.__module__ == "bkg_py.workspace.index"
    assert GitControlRefRepository.__module__ == "bkg_py.workspace.handoff"
    assert GitLocalConfiguration.__module__ == "bkg_py.workspace.merge_configuration"
    assert GitBranchPublisher.__module__ == "bkg_py.workspace.publication"
    assert not hasattr(GitSourceRepository, "materialize_sparse_paths")
    assert not hasattr(GitIndexRepository, "remote_url")
    exports = set(bkg_py.workspace.__all__)
    assert {"GitIndexRepository", "GitSourceRepository"} <= exports
    assert "GitRepository" not in exports
    assert "GitControlRefRepository" not in exports
    assert "GitBranchPublisher" not in exports
    assert "GitLocalConfiguration" not in exports


def test_workspace_has_no_generic_repository_implementation() -> None:
    """The package no longer carries the former all-purpose adapter module."""

    specification = find_spec("bkg_py.workspace")
    assert specification is not None
    assert specification.origin is not None
    package = Path(specification.origin).parent
    assert not (package / "repository.py").exists()
    assert find_spec("bkg_py.workspace.repository") is None
    assert all(
        "GitRepository" not in source.read_text(encoding="utf-8")
        for source in package.glob("*.py")
    )
