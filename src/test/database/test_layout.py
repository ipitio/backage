"""Structural tests for focused SQLite repositories and shared primitives."""

from __future__ import annotations

import ast
from importlib.util import find_spec, resolve_name
from pathlib import Path

import bkg_py.database
from bkg_py.database import DatabaseError, DatabaseRepositories, DatabaseSettings

from ..layout_support import package_path

_ROOT_FILES = {
    "__init__.py",
    "composition.py",
    "kernel.py",
    "models.py",
    "settings.py",
    "support.py",
    "values.py",
}
_CLUSTER_FILES = {
    "catalog": {
        "__init__.py",
        "dashboard.py",
        "dashboard_repository.py",
        "package_repository.py",
        "packages.py",
    },
    "history": {
        "__init__.py",
        "benchmark.py",
        "layout.py",
        "migration.py",
        "package_history.py",
        "packages.py",
        "repository.py",
        "version_history.py",
        "version_stages.py",
    },
    "maintenance": {
        "__init__.py",
        "metrics.py",
        "metrics_repository.py",
        "rotation_repository.py",
        "rotations.py",
    },
    "owner": {
        "__init__.py",
        "identities.py",
        "planning.py",
        "queue.py",
        "queue_repository.py",
        "scan_repository.py",
        "scans.py",
    },
    "package": {
        "__init__.py",
        "planning.py",
        "progress.py",
        "records.py",
        "rendering.py",
        "repository.py",
    },
    "schema": {"__init__.py", "lifecycle.py", "sql.py"},
}
_REMOVED_FLAT_MODULES = {
    "batch_progress",
    "catalog_repository",
    "dashboard",
    "dashboard_repository",
    "history_benchmark",
    "history_layout",
    "history_migration",
    "history_packages",
    "history_repository",
    "metrics",
    "metrics_repository",
    "owner_identities",
    "owner_plans",
    "owner_queue",
    "owner_queue_repository",
    "owner_repository",
    "owner_scans",
    "package_history",
    "package_plans",
    "package_repository",
    "packages",
    "render_sql",
    "rotation_events",
    "rotation_repository",
    "schema_sql",
    "version_history",
    "version_stages",
}


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


def test_database_physical_hierarchy_is_curated() -> None:
    """Stable capabilities have real packages without flat compatibility paths."""

    database = package_path("bkg_py.database")
    assert _python_files(database) == _ROOT_FILES
    assert {
        child.name
        for child in database.iterdir()
        if child.is_dir() and child.name != "__pycache__"
    } == set(_CLUSTER_FILES)

    for cluster, expected in _CLUSTER_FILES.items():
        cluster_path = database / cluster
        assert _python_files(cluster_path) == expected
        tree = ast.parse((cluster_path / "__init__.py").read_text(encoding="utf-8"))
        assert not any(
            isinstance(node, (ast.Import, ast.ImportFrom)) for node in tree.body
        )

    for module in _REMOVED_FLAT_MODULES:
        assert find_spec(f"bkg_py.database.{module}") is None
    for cluster in _CLUSTER_FILES:
        specification = find_spec(f"bkg_py.database.{cluster}")
        assert specification is not None
        assert specification.submodule_search_locations is not None


def test_database_composition_shares_one_kernel(tmp_path: Path) -> None:
    """Focused repositories share connection policy, schema state, and counters."""

    repositories = DatabaseRepositories(DatabaseSettings(tmp_path / "index.db"))
    components = (
        (repositories.packages, "bkg_py.database.package.repository"),
        (repositories.owners, "bkg_py.database.owner.scan_repository"),
        (repositories.owner_identities, "bkg_py.database.owner.identities"),
        (repositories.owner_queue, "bkg_py.database.owner.queue_repository"),
        (repositories.catalog, "bkg_py.database.catalog.package_repository"),
        (repositories.dashboard, "bkg_py.database.catalog.dashboard_repository"),
        (repositories.history, "bkg_py.database.history.repository"),
        (repositories.metrics, "bkg_py.database.maintenance.metrics_repository"),
        (repositories.rotations, "bkg_py.database.maintenance.rotation_repository"),
    )

    for component, module in components:
        assert component.kernel is repositories.kernel
        assert type(component).__module__ == module


def test_database_import_graph_is_acyclic() -> None:
    """Physical capability boundaries retain an acyclic concrete module graph."""

    graph = _database_import_graph(package_path("bkg_py.database"))
    pending = {module: set(dependencies) for module, dependencies in graph.items()}
    while pending:
        leaves = {
            module for module, dependencies in pending.items() if not dependencies
        }
        assert leaves, pending
        pending = {
            module: dependencies - leaves
            for module, dependencies in pending.items()
            if module not in leaves
        }


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

    source_text = "\n".join(
        source.read_text(encoding="utf-8") for source in database.rglob("*.py")
    )
    assert not broad_imports
    assert "DatabaseRepository" not in source_text
    assert "RepositoryMixin" not in source_text
    assert "class _SqlIdentifier" not in source_text
    assert source_text.count("SQLite identifiers cannot contain NUL") == 1
    assert "def _transaction" not in source_text


def _python_files(path: Path) -> set[str]:
    return {source.name for source in path.glob("*.py")}


def _database_import_graph(database: Path) -> dict[str, set[str]]:
    sources = tuple(database.rglob("*.py"))
    modules = {_module_name(database, source): source for source in sources}
    graph = {module: set[str]() for module in modules}
    for module, source in modules.items():
        package = module if source.name == "__init__.py" else module.rpartition(".")[0]
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                graph[module].update(
                    name
                    for alias in node.names
                    if (name := _known_module(alias.name, modules)) is not None
                )
            elif isinstance(node, ast.ImportFrom):
                imported = _resolved_import(node, package)
                for alias in node.names:
                    candidate = f"{imported}.{alias.name}"
                    dependency = candidate if candidate in modules else imported
                    if dependency in modules:
                        graph[module].add(dependency)
        graph[module].discard(module)
    return graph


def _module_name(database: Path, source: Path) -> str:
    parts = source.relative_to(database).with_suffix("").parts
    if parts[-1] == "__init__":
        parts = parts[:-1]
    suffix = ".".join(parts)
    return "bkg_py.database" + (f".{suffix}" if suffix else "")


def _resolved_import(node: ast.ImportFrom, package: str) -> str:
    if node.level:
        return resolve_name(f"{'.' * node.level}{node.module or ''}", package)
    return node.module or ""


def _known_module(name: str, modules: dict[str, Path]) -> str | None:
    candidates = (
        module for module in modules if name == module or name.startswith(f"{module}.")
    )
    return max(candidates, key=len, default=None)
