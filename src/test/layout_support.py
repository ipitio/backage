"""Shared import-boundary helpers for structural package tests."""

from __future__ import annotations

import ast
from importlib.util import find_spec
from pathlib import Path

_PARENT_PACKAGE_LEVEL = 2


def package_path(module: str) -> Path:
    """Return the directory containing one imported package."""

    specification = find_spec(module)
    if specification is None or specification.origin is None:
        raise ValueError(f"module does not identify an imported package: {module}")
    return Path(specification.origin).parent


def forbidden_imports(
    node: ast.Import | ast.ImportFrom,
    forbidden: frozenset[str],
) -> tuple[str, ...]:
    """Return forbidden top-level bkg dependencies referenced by an import."""

    if isinstance(node, ast.Import):
        roots = (
            alias.name.removeprefix("bkg_py.").split(".", maxsplit=1)[0]
            for alias in node.names
            if alias.name.startswith("bkg_py.")
        )
        return tuple(root for root in roots if root in forbidden)

    module = node.module or ""
    if node.level >= _PARENT_PACKAGE_LEVEL:
        root = module.split(".", maxsplit=1)[0]
        return (root,) if root in forbidden else ()
    if module == "bkg_py":
        blocked = {alias.name for alias in node.names} & forbidden
        return tuple(sorted(blocked))
    if module.startswith("bkg_py."):
        root = module.removeprefix("bkg_py.").split(".", maxsplit=1)[0]
        return (root,) if root in forbidden else ()
    return ()
