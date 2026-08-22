"""Structural tests for focused SQLite repositories and shared primitives."""

from __future__ import annotations

import ast
from importlib.util import find_spec
from pathlib import Path

import bkg_py.database
from bkg_py.database import DatabaseError, DatabaseRepositories, DatabaseSettings

from ..layout_support import package_path


def test_database_root_exposes_only_composition_settings_and_failures() -> None:
    """Callers do not regain the former repository and value re-export facade."""

    assert set(bkg_py.database.__all__) == {
        "DatabaseError",
        "DatabaseRepositories",
        "DatabaseSettings",
    }
    assert DatabaseError.__module__ == "bkg_py.database.support"
    assert DatabaseRepositories.__module__ == "bkg_py.database.composition"
    assert DatabaseSettings.__module__ == "bkg_py.database.settings"
    assert find_spec("bkg_py.database.repository") is None
    assert not hasattr(bkg_py.database, "DatabaseRepository")


def test_database_composition_shares_one_kernel(tmp_path: Path) -> None:
    """Focused repositories share connection policy, schema state, and counters."""

    repositories = DatabaseRepositories(DatabaseSettings(tmp_path / "index.db"))
    components = (
        (repositories.packages, "bkg_py.database.package_repository"),
        (repositories.owners, "bkg_py.database.owner_repository"),
        (repositories.owner_identities, "bkg_py.database.owner_identities"),
        (repositories.owner_queue, "bkg_py.database.owner_queue_repository"),
        (repositories.catalog, "bkg_py.database.catalog_repository"),
        (repositories.dashboard, "bkg_py.database.dashboard_repository"),
        (repositories.history, "bkg_py.database.history_repository"),
        (repositories.metrics, "bkg_py.database.metrics_repository"),
        (repositories.rotations, "bkg_py.database.rotation_repository"),
    )

    for component, module in components:
        assert component.kernel is repositories.kernel
        assert type(component).__module__ == module


def test_database_consumers_and_sql_primitives_remain_narrow() -> None:
    """Consumers import concrete modules and SQL safety helpers stay centralized."""

    database = package_path("bkg_py.database")
    package = database.parent
    broad_imports: list[tuple[Path, int]] = []
    for source in package.rglob("*.py"):
        if source.is_relative_to(database):
            continue
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        broad_imports.extend(
            (source, node.lineno)
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            and node.module in {"bkg_py.database", "database"}
        )

    database_sources = tuple(database.glob("*.py"))
    source_text = "\n".join(
        source.read_text(encoding="utf-8") for source in database_sources
    )
    assert not broad_imports
    assert "DatabaseRepository" not in source_text
    assert "RepositoryMixin" not in source_text
    assert "class _SqlIdentifier" not in source_text
    assert source_text.count("SQLite identifiers cannot contain NUL") == 1
    assert "def _transaction" not in source_text
