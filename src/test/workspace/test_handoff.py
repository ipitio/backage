"""Tests for isolated Git workflow handoff signaling."""

import io
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
    scheduled_update_skip_reason,
    workflow_run_freshness,
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
    settings = HandoffSettings.from_mapping(
        {
            "GITHUB_ACTOR": "test",
            "GITHUB_RUN_ID": "123",
            "BKG_HANDOFF_CONTROL_REF": _CONTROL_REF,
        }
    )
    monkeypatch.setenv("GITHUB_ACTOR", "changed-after-capture")
    monkeypatch.setenv("GITHUB_RUN_ID", "999")
    control = WorkflowHandoffControl(
        signaler,
        settings,
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
    assert git(signaler, "show", "-s", "--format=%s", first).stdout.strip() == (
        "Request workflow handoff (123)"
    )
    assert git(signaler, "show", "-s", "--format=%an <%ae>", first).stdout.strip() == (
        "test <test@users.noreply.github.com>"
    )

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


def test_handoff_request_accepts_a_concurrent_advance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Another request advancing the ref satisfies the same stop objective."""

    _seed, writer, signaler = _handoff_repositories(tmp_path)
    _control(writer).request()
    messages: list[str] = []
    control = _control(signaler, progress=messages.append)
    baseline = control.current_baseline()

    def lose_push_race(
        _commit: str,
        _ref: str,
        *,
        remote: str = "origin",
        force_with_lease: str | None = None,
    ) -> bool:
        assert remote == "origin"
        assert force_with_lease is None
        _control(writer).request()
        return False

    monkeypatch.setattr(control.repository, "push_ref", lose_push_race)

    control.request()

    assert control.current_baseline() != baseline
    assert messages == ["Graceful handoff was already requested concurrently"]


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


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        (
            ("current", "current", "100", "100", ""),
            None,
        ),
        (
            ("current", "current", "100", "99", ""),
            None,
        ),
        (
            ("queued", "current", "100", "100", ""),
            "a Manual handoff was requested after this run queued",
        ),
        (
            ("current", "current", "100", "101", ""),
            "scheduled run 101 supersedes 100",
        ),
        (
            ("current", "current", "100", "100", "200"),
            "Manual run 200 is waiting",
        ),
    ],
)
def test_scheduled_update_yields_only_to_newer_or_manual_work(
    values: tuple[str, str, str, str, str],
    expected: str | None,
) -> None:
    """Queue admission preserves the serialized update priority rules."""

    reason = scheduled_update_skip_reason(*values)

    if expected is None:
        assert reason is None
    else:
        assert reason is not None
        assert expected in reason


def test_handoff_should_run_cli_returns_nonfatal_for_a_superseded_run(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The workflow command prints its skip reason and returns status one."""

    status = main(
        [
            "handoff",
            "should-run",
            "current",
            "current",
            "100",
            "101",
            "",
        ]
    )

    assert status is ExitStatus.NON_FATAL
    assert "scheduled run 101 supersedes 100" in capsys.readouterr().out


def test_workflow_run_freshness_selects_current_update_and_manual() -> None:
    """Actions response parsing ignores unrelated and completed runs."""

    result = workflow_run_freshness(
        {
            "workflow_runs": [
                {
                    "id": 100,
                    "event": "schedule",
                    "path": ".github/workflows/update.yml",
                    "status": "completed",
                },
                {
                    "id": 101,
                    "event": "schedule",
                    "path": ".github/workflows/update.yml",
                    "status": "queued",
                },
                {
                    "id": 200,
                    "event": "workflow_dispatch",
                    "path": ".github/workflows/manual.yml",
                    "status": "completed",
                },
                {
                    "id": 201,
                    "event": "workflow_dispatch",
                    "path": ".github/workflows/manual.yml",
                    "status": "in_progress",
                },
                {
                    "id": 300,
                    "event": "schedule",
                    "path": ".github/workflows/vacuum.yml",
                    "status": "queued",
                },
            ]
        }
    )

    assert result == ("101", "201")


def test_workflow_runs_cli_prints_delimited_empty_values(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The workflow can split an empty freshness selection without jq."""

    monkeypatch.setattr("sys.stdin", io.StringIO('{"workflow_runs":[]}'))

    status = main(["handoff", "workflow-runs"])

    assert status is ExitStatus.SUCCESS
    assert capsys.readouterr().out == "|\n"


def test_workflow_run_freshness_rejects_an_invalid_response() -> None:
    """A malformed Actions response takes the workflow's fail-open path."""

    with pytest.raises(ValueError, match="workflow_runs"):
        workflow_run_freshness({"unexpected": []})
