"""Tests for isolated Git workflow handoff signaling."""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

import pytest

from bkg_py.cli import main
from bkg_py.result import ExitStatus
from bkg_py.runtime import StopController
from bkg_py.state import StateStore
from bkg_py.workspace import (
    HandoffSettings,
    WorkflowHandoffControl,
    WorkspaceError,
)

from .repository_support import clone_repository, create_repository_with_remote, git

_CONTROL_REF = "refs/heads/bkg-control"
_MARKER = "Bkg-Control-Format: isolated-v1"


def _handoff_repositories(tmp_path: Path) -> tuple[Path, Path, Path]:
    seed, remote = create_repository_with_remote(tmp_path)
    writer = tmp_path / "writer"
    signaler = tmp_path / "signaler"
    clone_repository(remote, writer)
    clone_repository(remote, signaler)
    return seed, writer, signaler


def _control(
    path: Path,
    *,
    progress: Callable[[str], None] | None = None,
    diagnostic: Callable[[str], None] | None = None,
) -> WorkflowHandoffControl:
    return WorkflowHandoffControl(
        path,
        HandoffSettings(_CONTROL_REF, poll_seconds=0.01),
        progress=progress,
        diagnostic=diagnostic,
    )


def test_handoff_request_creates_and_advances_isolated_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Control requests use an empty-tree chain detached from source history."""

    _seed, writer, signaler = _handoff_repositories(tmp_path)
    messages: list[str] = []
    monkeypatch.setenv("GITHUB_ACTOR", "test")
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    control = WorkflowHandoffControl(
        signaler,
        HandoffSettings(_CONTROL_REF),
        progress=messages.append,
    )

    assert _control(writer).current_baseline() == "missing"
    source_head = git(signaler, "rev-parse", "HEAD").stdout.strip()
    control.request()
    first = _control(writer).current_baseline()
    empty_tree = git(signaler, "mktree").stdout.strip()

    assert git(signaler, "rev-parse", "HEAD").stdout.strip() == source_head
    assert git(signaler, "show", "-s", "--format=%P", first).stdout.strip() == ""
    assert git(signaler, "show", "-s", "--format=%T", first).stdout.strip() == (
        empty_tree
    )
    assert _MARKER in git(signaler, "show", "-s", "--format=%B", first).stdout

    control.request()
    second = _control(writer).current_baseline()

    assert second != first
    assert git(signaler, "show", "-s", "--format=%P", second).stdout.strip() == (first)
    assert messages == [
        "Requested graceful handoff from the active update",
        "Requested graceful handoff from the active update",
    ]


def test_handoff_request_migrates_legacy_history(tmp_path: Path) -> None:
    """An existing source-linked control ref is replaced with isolated history."""

    seed, writer, signaler = _handoff_repositories(tmp_path)
    git(seed, "push", "--quiet", "origin", f"HEAD:{_CONTROL_REF}")
    legacy = _control(writer).current_baseline()
    messages: list[str] = []

    _control(signaler, progress=messages.append).request()
    current = _control(writer).current_baseline()

    assert current != legacy
    assert git(signaler, "show", "-s", "--format=%P", current).stdout.strip() == ""
    assert _MARKER in git(signaler, "show", "-s", "--format=%B", current).stdout
    assert messages == [
        "Migrated workflow handoff ref to isolated history",
        "Requested graceful handoff from the active update",
    ]


def test_handoff_request_preserves_protected_legacy_history(
    tmp_path: Path,
) -> None:
    """A remote that rejects migration still accepts a safe fast-forward request."""

    seed, writer, signaler = _handoff_repositories(tmp_path)
    remote = tmp_path / "remote.git"
    git(seed, "push", "--quiet", "origin", f"HEAD:{_CONTROL_REF}")
    legacy = _control(writer).current_baseline()
    git(remote, "config", "receive.denyNonFastforwards", "true")
    diagnostics: list[str] = []

    _control(signaler, diagnostic=diagnostics.append).request()
    current = _control(writer).current_baseline()

    assert git(signaler, "show", "-s", "--format=%P", current).stdout.strip() == (
        legacy
    )
    assert (
        _MARKER
        not in git(
            signaler,
            "show",
            "-s",
            "--format=%B",
            current,
        ).stdout
    )
    assert diagnostics == [
        "Workflow handoff ref could not be isolated; preserving its existing history"
    ]


def test_handoff_monitor_requests_shared_stop(tmp_path: Path) -> None:
    """An advanced control ref wakes the application stop controller once."""

    _seed, writer, signaler = _handoff_repositories(tmp_path)
    active = _control(writer)
    baseline = active.current_baseline()
    state = StateStore(tmp_path / "env.env")
    state.path.touch()
    stop = StopController(state, max_duration=-1)

    with active.monitor(baseline, stop):
        _control(signaler).request()
        deadline = time.monotonic() + 2
        while not stop.is_requested() and time.monotonic() < deadline:
            time.sleep(0.01)

    assert stop.reason == "handoff"
    assert state.get("BKG_TIMEOUT") == "1"


def test_handoff_rejects_non_branch_control_ref(tmp_path: Path) -> None:
    """Tags cannot be mutated through the workflow control interface."""

    control = WorkflowHandoffControl(
        tmp_path,
        HandoffSettings("refs/tags/not-allowed"),
    )

    with pytest.raises(WorkspaceError, match="refs/heads"):
        control.current_baseline()


def test_handoff_cli_reports_missing_baseline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The workflow-facing command retains the shell baseline output shape."""

    _seed, writer, _signaler = _handoff_repositories(tmp_path)
    monkeypatch.setenv("BKG_HANDOFF_CONTROL_REF", _CONTROL_REF)

    status = main(["handoff", "baseline", str(writer)])

    assert status is ExitStatus.SUCCESS
    assert capsys.readouterr().out == "missing\n"
