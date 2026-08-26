"""Structural tests for the publication infrastructure package."""

import ast
from importlib.util import find_spec
from pathlib import Path

import bkg_py.publication
from bkg_py.publication import (
    JsonValue,
    PublicationError,
    PublicationLimits,
    PublicationResult,
    publish_json_file,
    write_xml_file,
    xml_chunks,
)

from ..layout_support import forbidden_imports, package_path

_FORMER_FLAT_MODULES = (
    "dashboard",
    "rendering",
    "release",
    "release_retention",
    "site_shell",
)
_FORBIDDEN_DEPENDENCIES = frozenset(
    {
        "application",
        "owners",
        "packages",
        "run",
        "snapshots",
        "workspace",
    }
)


def test_publication_root_exposes_only_artifact_primitives() -> None:
    """The package root remains the small shared artifact API."""

    exported = {
        PublicationError,
        PublicationLimits,
        PublicationResult,
        publish_json_file,
        write_xml_file,
        xml_chunks,
    }
    assert {value.__module__ for value in exported} == {"bkg_py.publication.artifacts"}
    assert JsonValue is not None
    assert set(bkg_py.publication.__all__) == {
        "JsonValue",
        "PublicationError",
        "PublicationLimits",
        "PublicationResult",
        "publish_json_file",
        "write_xml_file",
        "xml_chunks",
    }


def test_publication_is_a_package_without_flat_companion_modules() -> None:
    """The structural move leaves no flat compatibility implementations."""

    package = package_path("bkg_py.publication")
    specification = find_spec("bkg_py.publication")
    assert specification is not None
    assert specification.submodule_search_locations is not None
    assert package.name == "publication"
    assert not package.with_suffix(".py").exists()
    assert all(find_spec(f"bkg_py.{module}") is None for module in _FORMER_FLAT_MODULES)


def test_publication_does_not_depend_on_domain_coordinators() -> None:
    """Output infrastructure remains below application and domain orchestration."""

    dependencies: list[tuple[Path, str]] = []
    for source in package_path("bkg_py.publication").glob("*.py"):
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        dependencies.extend(
            (source, dependency)
            for node in ast.walk(tree)
            if isinstance(node, ast.Import | ast.ImportFrom)
            for dependency in forbidden_imports(node, _FORBIDDEN_DEPENDENCIES)
        )

    assert not dependencies
