"""Tests for repository workspace preparation."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from bkg_py.cli import main
from bkg_py.result import ExitStatus
from bkg_py.workspace import (
    GitRepository,
    WorkspaceError,
    WorkspaceLayout,
    import_workflow_payload,
)
from bkg_py.workspace.repository import ensure_pages_root


def _git(repository: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    git = shutil.which("git")
    assert git is not None
    return subprocess.run(  # noqa: S603
        (git, "-C", str(repository), *arguments),
        check=True,
        capture_output=True,
        text=True,
    )


def _create_repository(path: Path) -> None:
    path.mkdir()
    git = shutil.which("git")
    assert git is not None
    subprocess.run(  # noqa: S603
        (git, "init", "-q", "-b", "master", str(path)),
        check=True,
    )
    _git(path, "config", "user.name", "test")
    _git(path, "config", "user.email", "test@example.com")
    (path / "alpha" / "repo-a").mkdir(parents=True)
    (path / "beta" / "repo-b").mkdir(parents=True)
    (path / ".env").write_text("state\n", encoding="utf-8")
    (path / "README.md").write_text("index\n", encoding="utf-8")
    (path / "alpha" / "repo-a" / "package.json").write_text(
        "{}\n",
        encoding="utf-8",
    )
    (path / "beta" / "repo-b" / "package.json").write_text(
        "{}\n",
        encoding="utf-8",
    )
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", "init")


def test_workspace_layout_uses_master_index_paths(tmp_path: Path) -> None:
    """The default source branch uses the established index names."""

    layout = WorkspaceLayout.from_branches(
        tmp_path,
        source_branch="master",
    )

    assert layout.github_branch == "master"
    assert layout.index_name == "index"
    assert layout.index_db == tmp_path / "index.db"
    assert layout.index_sql == tmp_path / "index.sql"
    assert layout.index_dir == tmp_path / "index"


def test_workspace_layout_prefers_requested_non_master_branch(tmp_path: Path) -> None:
    """An explicit workflow branch determines its matching index branch."""

    layout = WorkspaceLayout.from_branches(
        tmp_path,
        source_branch="master",
        requested_branch="development",
    )

    assert layout.source_branch == "master"
    assert layout.github_branch == "development"
    assert layout.index_name == "index-development"


def test_workspace_layout_supports_detached_checkout_with_requested_branch(
    tmp_path: Path,
) -> None:
    """A configured branch keeps detached workflow checkouts unambiguous."""

    repository = tmp_path / "repository"
    _create_repository(repository)
    _git(repository, "checkout", "-q", "--detach")

    layout = WorkspaceLayout.discover(repository, "development")

    assert layout.source_branch == ""
    assert layout.github_branch == "development"
    assert layout.index_name == "index-development"


def test_workspace_layout_rejects_unidentified_detached_checkout(
    tmp_path: Path,
) -> None:
    """A detached checkout needs an explicit source branch."""

    repository = tmp_path / "repository"
    _create_repository(repository)
    _git(repository, "checkout", "-q", "--detach")

    with pytest.raises(WorkspaceError, match="set GITHUB_BRANCH"):
        WorkspaceLayout.discover(repository)


def test_workspace_layout_cli_emits_nul_delimited_shell_fields(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The launcher receives paths without relying on whitespace splitting."""

    repository = tmp_path / "repository"
    _create_repository(repository)

    status = main(["workspace", "layout", str(repository)])

    assert status == ExitStatus.SUCCESS
    assert capsys.readouterr().out.split("\0") == [
        "master",
        "master",
        "index",
        str(repository / "index.db"),
        str(repository / "index.sql"),
        str(repository / "index"),
        "",
    ]


def test_workflow_payload_import_merges_directories_and_replaces_files(
    tmp_path: Path,
) -> None:
    """Downloaded snapshots and dotfiles replace matching repository entries."""

    payload = tmp_path / ".bkg"
    destination = tmp_path / "repository"
    (payload / ".snapshot").mkdir(parents=True)
    (destination / ".snapshot").mkdir(parents=True)
    (payload / ".snapshot" / "index.db").write_text("new", encoding="utf-8")
    (payload / ".hidden").write_text("payload", encoding="utf-8")
    (destination / ".snapshot" / "index.db").write_text("old", encoding="utf-8")
    (destination / ".snapshot" / "retained").write_text(
        "keep",
        encoding="utf-8",
    )

    import_workflow_payload(payload, destination)

    assert (destination / ".snapshot" / "index.db").read_text() == "new"
    assert (destination / ".snapshot" / "retained").read_text() == "keep"
    assert (destination / ".hidden").read_text() == "payload"
    assert not list(payload.iterdir())


def test_missing_workflow_payload_is_a_no_op(tmp_path: Path) -> None:
    """Local runs do not need a downloaded workflow payload."""

    destination = tmp_path / "repository"

    import_workflow_payload(tmp_path / "missing", destination)

    assert not destination.exists()


def test_sparse_repository_keeps_root_and_materializes_selected_owners(
    tmp_path: Path,
) -> None:
    """Sparse checkout can inspect all owners while hydrating only queued trees."""

    path = tmp_path / "index"
    _create_repository(path)
    repository = GitRepository(path)

    assert repository.top_level_directory_count() == 2
    repository.set_sparse_root()

    assert (path / ".env").is_file()
    assert (path / "README.md").is_file()
    assert not (path / "alpha").exists()
    assert not (path / "beta").exists()
    assert repository.top_level_directory_count() == 2

    repository.add_sparse_paths(("alpha", "beta"))

    assert (path / "alpha" / "repo-a" / "package.json").is_file()
    assert (path / "beta" / "repo-b" / "package.json").is_file()


def test_sparse_operations_ignore_non_repository_path(tmp_path: Path) -> None:
    """Optional local index paths retain the previous no-op behavior."""

    repository = GitRepository(tmp_path / "missing")

    assert not repository.is_worktree()
    repository.set_sparse_root()
    repository.add_sparse_paths(("alpha",))
    assert repository.top_level_directory_count() == 0


def test_ensure_pages_root_atomically_writes_empty_marker(tmp_path: Path) -> None:
    """The index root always disables Jekyll processing for dotfiles."""

    root = tmp_path / "index"
    ensure_pages_root(root)
    marker = root / ".nojekyll"
    marker.write_text("stale", encoding="utf-8")

    ensure_pages_root(root)

    assert marker.read_bytes() == b""
