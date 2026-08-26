"""Tests for Python 3.14 native deferred annotations."""

import ast
from annotationlib import Format, ForwardRef, get_annotations
from dataclasses import fields
from pathlib import Path

from bkg_py.concurrency import ConcurrencySettings
from bkg_py.database.models import OwnerScanResult, PackageCatalogPath, PackageRef

_SOURCE_ROOT = Path(__file__).parents[1]


def test_python_tree_uses_native_deferred_annotations() -> None:
    """Python 3.14 owns deferred evaluation without stringizing imports."""

    stringized_modules: list[str] = []
    for path in sorted(_SOURCE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if any(
            isinstance(node, ast.ImportFrom)
            and node.module == "__future__"
            and any(name.name == "annotations" for name in node.names)
            for node in tree.body
        ):
            stringized_modules.append(str(path.relative_to(_SOURCE_ROOT)))

    assert not stringized_modules


def test_native_annotations_resolve_later_dataclass_references() -> None:
    """Deferred class annotations resolve after their module is initialized."""

    annotations = get_annotations(OwnerScanResult, format=Format.VALUE)

    assert annotations["removed"] == tuple[PackageRef, ...]
    assert annotations["catalog_removed"] == tuple[PackageCatalogPath, ...]
    assert {field.name: field for field in fields(OwnerScanResult)}[
        "catalog_removed"
    ].default == ()
    assert not OwnerScanResult((), (), 0).catalog_removed


def test_unresolved_type_checking_annotations_remain_inspectable() -> None:
    """Forward-reference inspection does not import optional runtime owners."""

    annotations = get_annotations(
        ConcurrencySettings.from_config,
        format=Format.FORWARDREF,
    )

    assert isinstance(annotations["config"], ForwardRef)
    assert annotations["config"].__forward_arg__ == "RuntimeConfig"
    assert annotations["return"] is ConcurrencySettings
