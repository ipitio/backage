"""Tests for unattended deployment-fork source synchronization."""

from pathlib import Path

import pytest

from bkg_py.cli import main
from bkg_py.result import ExitStatus
from bkg_py.workspace.fork_sync import synchronize_fork_source
from bkg_py.workspace.git import WorkspaceError

from .repository_support import clone_repository, create_repository, git


def _create_fork_pair(tmp_path: Path) -> tuple[Path, Path]:
    upstream = tmp_path / "upstream"
    create_repository(upstream)
    (upstream / "owners.txt").write_text("main-owner\n", encoding="utf-8")
    (upstream / "optout.txt").write_text("main/package\n", encoding="utf-8")
    (upstream / "application.py").write_text("value = 'base'\n", encoding="utf-8")
    git(upstream, "add", "-A")
    git(upstream, "commit", "-qm", "add deployment source")

    fork = tmp_path / "fork"
    clone_repository(upstream, fork)
    git(fork, "config", "user.name", "test")
    git(fork, "config", "user.email", "test@example.com")
    return upstream, fork


def _customize_fork_inputs(fork: Path) -> None:
    (fork / "owners.txt").write_text("fork-owner\n", encoding="utf-8")
    (fork / "optout.txt").write_text("fork/package\n", encoding="utf-8")
    (fork / "README.md").write_text("fork rendering\n", encoding="utf-8")
    git(fork, "commit", "-qam", "customize deployment inputs")


def test_workflow_sync_skips_upstream_deployment_churn(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Main-only deployment edits do not rebuild an otherwise current fork."""

    upstream, fork = _create_fork_pair(tmp_path)
    _customize_fork_inputs(fork)
    fork_head = git(fork, "rev-parse", "HEAD").stdout.strip()
    (upstream / "owners.txt").write_text("next-main-owner\n", encoding="utf-8")
    (upstream / "optout.txt").write_text("next-main/package\n", encoding="utf-8")
    (upstream / "README.md").write_text("next main rendering\n", encoding="utf-8")
    git(upstream, "commit", "-qam", "refresh main deployment inputs")
    upstream_head = git(upstream, "rev-parse", "HEAD").stdout.strip()

    status = main(
        [
            "workflow-sync-fork",
            "-C",
            str(fork),
            "-u",
            str(upstream),
            "-b",
            "master",
        ]
    )

    assert status is ExitStatus.SUCCESS
    assert capsys.readouterr().out.splitlines() == [
        "updated=false",
        f"upstream_sha={upstream_head}",
    ]
    assert git(fork, "rev-parse", "HEAD").stdout.strip() == fork_head
    assert (fork / "owners.txt").read_text(encoding="utf-8") == "fork-owner\n"
    assert (fork / "optout.txt").read_text(encoding="utf-8") == "fork/package\n"
    assert (fork / "README.md").read_text(encoding="utf-8") == "fork rendering\n"


def test_fork_sync_merges_source_and_preserves_deployment_inputs(
    tmp_path: Path,
) -> None:
    """A material source update merges while fork-owned files remain local."""

    upstream, fork = _create_fork_pair(tmp_path)
    _customize_fork_inputs(fork)
    (upstream / "owners.txt").write_text("next-main-owner\n", encoding="utf-8")
    (upstream / "optout.txt").write_text("next-main/package\n", encoding="utf-8")
    (upstream / "README.md").write_text("next main rendering\n", encoding="utf-8")
    (upstream / "application.py").write_text(
        "value = 'upstream'\n",
        encoding="utf-8",
    )
    git(upstream, "commit", "-qam", "update upstream application")

    result = synchronize_fork_source(
        fork,
        upstream_url=str(upstream),
        upstream_branch="master",
    )

    assert result.updated
    assert result.upstream_commit == git(upstream, "rev-parse", "HEAD").stdout.strip()
    assert (fork / "owners.txt").read_text(encoding="utf-8") == "fork-owner\n"
    assert (fork / "optout.txt").read_text(encoding="utf-8") == "fork/package\n"
    assert (fork / "README.md").read_text(encoding="utf-8") == "fork rendering\n"
    assert (fork / "application.py").read_text(encoding="utf-8") == (
        "value = 'upstream'\n"
    )
    assert (
        git(
            fork,
            "merge-base",
            "--is-ancestor",
            result.upstream_commit,
            "HEAD",
        ).returncode
        == 0
    )
    assert not git(fork, "status", "--porcelain").stdout


def test_fork_sync_projects_upstream_over_conflicting_source_changes(
    tmp_path: Path,
) -> None:
    """Managed forks retain source history while adopting the upstream tree."""

    upstream, fork = _create_fork_pair(tmp_path)
    (fork / "application.py").write_text("value = 'fork'\n", encoding="utf-8")
    git(fork, "commit", "-qam", "customize fork application")
    fork_head = git(fork, "rev-parse", "HEAD").stdout.strip()
    (upstream / "application.py").write_text(
        "value = 'upstream'\n",
        encoding="utf-8",
    )
    git(upstream, "commit", "-qam", "change upstream application")

    result = synchronize_fork_source(
        fork,
        upstream_url=str(upstream),
        upstream_branch="master",
    )

    assert result.updated
    parents = git(fork, "show", "-s", "--format=%P", "HEAD").stdout.split()
    assert parents == [fork_head, result.upstream_commit]
    assert (fork / "application.py").read_text(encoding="utf-8") == (
        "value = 'upstream'\n"
    )
    assert not git(fork, "status", "--porcelain").stdout


def test_fork_sync_removes_legacy_source_deleted_upstream(tmp_path: Path) -> None:
    """A modified legacy path does not survive as an untracked file."""

    upstream, fork = _create_fork_pair(tmp_path)
    (upstream / "legacy.sh").write_text("old upstream source\n", encoding="utf-8")
    git(upstream, "add", "legacy.sh")
    git(upstream, "commit", "-qm", "add legacy source")
    git(fork, "fetch", "-q", "origin", "master")
    git(fork, "merge", "--ff-only", "origin/master")
    (fork / "legacy.sh").write_text("fork legacy source\n", encoding="utf-8")
    git(fork, "commit", "-qam", "customize legacy source")
    git(upstream, "rm", "-q", "legacy.sh")
    (upstream / "application.py").write_text(
        "value = 'current'\n",
        encoding="utf-8",
    )
    git(upstream, "commit", "-qam", "replace legacy source")

    result = synchronize_fork_source(
        fork,
        upstream_url=str(upstream),
        upstream_branch="master",
    )

    assert result.updated
    assert not (fork / "legacy.sh").exists()
    assert not git(fork, "status", "--porcelain").stdout


def test_fork_sync_adopts_defaults_for_missing_deployment_inputs(
    tmp_path: Path,
) -> None:
    """An old fork acquires deployment files that did not exist in its branch."""

    upstream, fork = _create_fork_pair(tmp_path)
    git(fork, "rm", "-q", "owners.txt", "optout.txt")
    git(fork, "commit", "-qm", "remove inputs unavailable in the old fork")
    (upstream / "application.py").write_text(
        "value = 'current'\n",
        encoding="utf-8",
    )
    git(upstream, "commit", "-qam", "update upstream source")

    result = synchronize_fork_source(
        fork,
        upstream_url=str(upstream),
        upstream_branch="master",
    )

    assert result.updated
    assert (fork / "owners.txt").read_text(encoding="utf-8") == "main-owner\n"
    assert (fork / "optout.txt").read_text(encoding="utf-8") == "main/package\n"
    assert not git(fork, "status", "--porcelain").stdout


def test_fork_sync_rejects_a_dirty_worktree(tmp_path: Path) -> None:
    """Managed projection never discards uncommitted deployment work."""

    upstream, fork = _create_fork_pair(tmp_path)
    (fork / "application.py").write_text("value = 'uncommitted'\n", encoding="utf-8")

    with pytest.raises(WorkspaceError, match="requires a clean worktree"):
        synchronize_fork_source(
            fork,
            upstream_url=str(upstream),
            upstream_branch="master",
        )
