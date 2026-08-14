"""Tests for the generated runtime ownership reference."""

from __future__ import annotations

import ast
import json
import re
import secrets
from collections.abc import Iterator
from pathlib import Path

import pytest

from bkg_py.cli import main
from bkg_py.result import ExitStatus
from bkg_py.runtime_names import EnvironmentVariable, RunFile, StateKey, StatePrefix

_PRODUCTION_SOURCE = Path(__file__).parents[1] / "bkg_py"
_RUNTIME_NAME = re.compile(r"(?:BKG|GITHUB|GH)_[A-Z0-9_]+")


def _production_trees() -> Iterator[tuple[Path, ast.Module]]:
    for path in _PRODUCTION_SOURCE.rglob("*.py"):
        if path.name in {"reference.py", "runtime_names.py"}:
            continue
        yield path, ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def test_reference_cli_reports_every_owned_runtime_surface_without_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The generated inventory is complete, redacted, and side-effect free."""

    token = secrets.token_urlsafe(12)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_TOKEN", token)

    assert main(["reference"]) is ExitStatus.SUCCESS
    captured = capsys.readouterr()
    reference = json.loads(captured.out)

    assert reference["schema_version"] == 1
    environment = {item["name"]: item for item in reference["environment"]}
    assert set(environment) == {variable.value for variable in EnvironmentVariable}
    assert environment["GITHUB_TOKEN"]["secret"] is True
    assert environment["BKG_MAX_LEN"]["owners"] == ["RuntimeConfig"]
    assert "BKG_BRANCH" not in environment
    assert token not in captured.out

    state = {item["name"]: item for item in reference["state"]}
    assert set(state) == {key.value for key in StateKey}
    assert state["BKG_INDEX_CLEANUP_DONE"]["lifecycle"] == "obsolete-delete"
    assert all(item["readers"] or item["writers"] for item in state.values())
    assert {item["pattern"] for item in reference["state_patterns"]} == {
        f"{prefix.value}*" for prefix in StatePrefix
    }

    commands = {item["command"]: item for item in reference["cli"]}
    assert {
        "bkg",
        "bkg config",
        "bkg configure-fork-merge",
        "bkg handoff",
        "bkg handoff baseline",
        "bkg handoff request",
        "bkg handoff should-run",
        "bkg handoff workflow-runs",
        "bkg reference",
        "bkg release-tag",
        "bkg run",
        "bkg vacuum-releases",
        "bkg validate",
        "bkg workflow-update",
    } == set(commands)
    run_arguments = {
        argument["name"]: argument for argument in commands["bkg run"]["arguments"]
    }
    assert run_arguments["source_published_today"]["forms"] == [
        "-s",
        "--source-published-today",
    ]

    schema = {item["name"] for item in reference["database_schema"]}
    assert {
        "owners",
        "bkg_history_packages",
        "bkg_history_package_observations",
        "bkg_package_history",
        "bkg_history_versions",
        "bkg_history_version_observations",
        "bkg_version_history",
        "bkg_owner_queue",
        "bkg_rotation_events",
    } <= schema
    paths = {item["path"] for item in reference["paths"]}
    assert "${BKG_INDEX_DIR}/.env" in paths
    assert "${BKG_ROOT}/.snapshot/${BKG_INDEX}.db" in paths
    assert "${BKG_INDEX_DIR}/{owner}/{repo}/{package}.json|.xml" in paths
    path_lifecycles = {item["path"]: item["lifecycle"] for item in reference["paths"]}
    assert path_lifecycles["${BKG_ROOT}/src/packages_all"] == "run-intermediate"
    assert path_lifecycles["${BKG_ROOT}/src/packages_to_update"] == "obsolete-delete"
    assert not list(tmp_path.iterdir())


def test_runtime_names_are_used_through_the_authoritative_enums() -> None:
    """New settings and state names cannot bypass inventory ownership."""

    used_environment: set[str] = set()
    used_run_files: set[str] = set()
    used_state: set[str] = set()
    used_prefixes: set[str] = set()
    raw_names: list[str] = []

    for path, tree in _production_trees():
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and _RUNTIME_NAME.fullmatch(node.value)
            ):
                raw_names.append(
                    f"{path.relative_to(_PRODUCTION_SOURCE)}:{node.lineno}"
                )
            if not isinstance(node, ast.Attribute) or not isinstance(
                node.value, ast.Name
            ):
                continue
            if node.value.id == "Env":
                used_environment.add(node.attr)
            elif node.value.id == "RunFile":
                used_run_files.add(node.attr)
            elif node.value.id == "StateKey":
                used_state.add(node.attr)
            elif node.value.id == "StatePrefix":
                used_prefixes.add(node.attr)

    assert not raw_names
    assert used_environment == set(EnvironmentVariable.__members__)
    assert used_run_files == set(RunFile.__members__)
    assert used_state == set(StateKey.__members__)
    assert used_prefixes == set(StatePrefix.__members__)


def test_reference_generator_is_imported_only_by_its_command_adapter() -> None:
    """Normal runtime imports remain independent of report generation."""

    reference_importers = {
        str(path.relative_to(_PRODUCTION_SOURCE))
        for path, tree in _production_trees()
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "reference"
    }
    assert reference_importers == {"commands.py"}
