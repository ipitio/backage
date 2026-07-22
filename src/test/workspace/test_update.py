"""Integration tests for the Python-owned outer update lifecycle."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from bkg_py.application import ApplicationContext
from bkg_py.result import ExitStatus
from bkg_py.run.commands import RunCommandOptions
from bkg_py.workspace import WorkflowHandoffControl
from bkg_py.workspace.update import (
    UpdateWorkflowExecution,
    UpdateWorkflowRequest,
    run_update_workflow,
)

from .repository_support import create_repository_with_remote, git


def _workflow_source(tmp_path: Path) -> tuple[Path, Path]:
    repository, remote = create_repository_with_remote(tmp_path)
    (repository / "src").mkdir()
    (repository / "src" / "env.env").write_text("", encoding="utf-8")
    (repository / "owners.txt").write_text("", encoding="utf-8")
    (repository / "optout.txt").write_text("", encoding="utf-8")
    git(repository, "add", "-A")
    git(repository, "commit", "-qm", "workflow source")
    git(repository, "push", "-q", "origin", "master")
    git(repository, "branch", "index")
    git(repository, "push", "-qu", "origin", "index")
    return repository, remote


def _snapshot_payload(invocation: Path) -> None:
    archive = invocation / ".bkg" / ".snapshot" / "index.db"
    archive.parent.mkdir(parents=True)
    with sqlite3.connect(archive) as database:
        database.execute("create table payload (value text)")
        database.execute("insert into payload values ('stored')")


def _set_workflow_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setenv("GITHUB_ACTOR", "test-actor")
    monkeypatch.setenv("GITHUB_OWNER", "example")
    monkeypatch.setenv("GITHUB_REPO", "backage")
    monkeypatch.setenv("GITHUB_BRANCH", "master")
    monkeypatch.delenv("BKG_HANDOFF_CONTROL_REF", raising=False)


def test_update_workflow_clones_restores_runs_and_publishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One service call carries downloaded state through both owning branches."""

    _source, remote = _workflow_source(tmp_path)
    invocation = tmp_path / "invocation"
    invocation.mkdir()
    _snapshot_payload(invocation)
    _set_workflow_environment(monkeypatch)
    monkeypatch.setenv("BKG_INDEX_DB", "outer.db")
    observed_options: list[RunCommandOptions] = []

    def run_application(
        options: RunCommandOptions,
        application: ApplicationContext,
        _handoff: WorkflowHandoffControl,
        baseline: str | None,
    ) -> ExitStatus:
        observed_options.append(options)
        assert baseline is None
        assert application.config.root == str(invocation / "checkout")
        assert application.config.index_name == "index"
        application.snapshots.prepare_database_snapshot()
        index_dir = Path(application.config.index_dir or "")
        (index_dir / "generated.json").write_text("{}\n", encoding="utf-8")
        (Path(application.config.root) / "README.md").write_text(
            "updated source\n",
            encoding="utf-8",
        )
        return ExitStatus.SUCCESS

    status = run_update_workflow(
        UpdateWorkflowRequest(
            root=Path("checkout"),
            invocation_directory=invocation,
            clone_url=remote.as_uri(),
        ),
        UpdateWorkflowExecution(run_application=run_application),
    )

    assert status is ExitStatus.SUCCESS
    assert len(observed_options) == 1
    assert observed_options[0].source_published_today
    assert observed_options[0].working_directory == invocation / "checkout/src"
    assert os.environ["BKG_INDEX_DB"] == "outer.db"
    assert git(remote, "show", "index:generated.json").stdout == "{}\n"
    assert git(remote, "show", "master:README.md").stdout == "updated source\n"
    assert (invocation / "checkout/.snapshot/index.db").stat().st_size > 100


def test_update_workflow_resets_published_stop_state_before_restoring(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed prior run cannot stop setup before the new run is configured."""

    source, remote = _workflow_source(tmp_path)
    git(source, "switch", "-q", "index")
    (source / ".env").write_text(
        "BKG_SCRIPT_START=1\nBKG_TIMEOUT=1\n",
        encoding="utf-8",
    )
    git(source, "add", ".env")
    git(source, "commit", "-qm", "stale run state")
    git(source, "push", "-q", "origin", "index")
    git(source, "switch", "-q", "master")

    invocation = tmp_path / "invocation"
    invocation.mkdir()
    _snapshot_payload(invocation)
    _set_workflow_environment(monkeypatch)
    ran = False

    def run_application(
        _options: RunCommandOptions,
        application: ApplicationContext,
        _handoff: WorkflowHandoffControl,
        _baseline: str | None,
    ) -> ExitStatus:
        nonlocal ran
        application.stop.check()
        assert application.state.get("BKG_TIMEOUT") == "0"
        assert int(application.state.get("BKG_SCRIPT_START") or "0") > 1
        application.snapshots.prepare_database_snapshot()
        ran = True
        return ExitStatus.SUCCESS

    status = run_update_workflow(
        UpdateWorkflowRequest(
            root=Path("checkout"),
            invocation_directory=invocation,
            clone_url=remote.as_uri(),
        ),
        UpdateWorkflowExecution(run_application=run_application),
    )

    assert status is ExitStatus.SUCCESS
    assert ran


def test_update_workflow_does_not_publish_nonfatal_application_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run that did not finalize successfully leaves both remotes unchanged."""

    _source, remote = _workflow_source(tmp_path)
    invocation = tmp_path / "invocation"
    invocation.mkdir()
    _snapshot_payload(invocation)
    _set_workflow_environment(monkeypatch)

    def run_application(
        _options: RunCommandOptions,
        application: ApplicationContext,
        _handoff: WorkflowHandoffControl,
        _baseline: str | None,
    ) -> ExitStatus:
        application.snapshots.prepare_database_snapshot()
        index_dir = Path(application.config.index_dir or "")
        (index_dir / "not-published.json").write_text("{}\n", encoding="utf-8")
        return ExitStatus.NON_FATAL

    status = run_update_workflow(
        UpdateWorkflowRequest(
            root=Path("checkout"),
            invocation_directory=invocation,
            clone_url=remote.as_uri(),
        ),
        UpdateWorkflowExecution(run_application=run_application),
    )

    assert status is ExitStatus.NON_FATAL
    assert git(remote, "show", "index:not-published.json", check=False).returncode != 0


@pytest.mark.parametrize(
    "snapshot_content", [None, b"x"], ids=("missing", "undersized")
)
def test_update_workflow_rejects_invalid_final_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    snapshot_content: bytes | None,
) -> None:
    """Publication cannot proceed without a complete final snapshot."""

    _source, remote = _workflow_source(tmp_path)
    invocation = tmp_path / "invocation"
    invocation.mkdir()
    _snapshot_payload(invocation)
    _set_workflow_environment(monkeypatch)
    diagnostics: list[str] = []

    def run_application(
        _options: RunCommandOptions,
        application: ApplicationContext,
        _handoff: WorkflowHandoffControl,
        _baseline: str | None,
    ) -> ExitStatus:
        archive = application.snapshots.prepare_database_snapshot()
        if snapshot_content is None:
            archive.unlink()
        else:
            archive.write_bytes(snapshot_content)
        return ExitStatus.SUCCESS

    status = run_update_workflow(
        UpdateWorkflowRequest(
            root=Path("checkout"),
            invocation_directory=invocation,
            clone_url=remote.as_uri(),
        ),
        UpdateWorkflowExecution(
            diagnostic=diagnostics.append,
            run_application=run_application,
        ),
    )

    assert status is ExitStatus.NON_FATAL
    assert diagnostics == ["prepared database snapshot is missing or undersized"]
