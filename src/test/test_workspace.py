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
    IndexWorkspacePreparer,
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


def _create_repository_with_remote(tmp_path: Path) -> tuple[Path, Path]:
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git(remote, "init", "--bare", "-q", "--initial-branch=master")
    repository = tmp_path / "repository"
    _create_repository(repository)
    _git(repository, "remote", "add", "origin", str(remote))
    _git(repository, "push", "-qu", "origin", "master")
    return repository, remote


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


def test_repository_configuration_keeps_token_out_of_git_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Future Git commands read credentials from the environment at invocation."""

    repository = tmp_path / "repository"
    _create_repository(repository)
    monkeypatch.setenv("GITHUB_TOKEN", "workflow-secret")

    GitRepository(repository).configure_for_updates("workflow-actor")

    config = (repository / ".git" / "config").read_text(encoding="utf-8")
    assert "workflow-secret" not in config
    assert "$GITHUB_TOKEN" in config
    assert _git(repository, "config", "user.name").stdout.strip() == "workflow-actor"
    assert _git(repository, "config", "core.untrackedcache").stdout.strip() == "true"

    git = shutil.which("git")
    assert git is not None
    credentials = subprocess.run(  # noqa: S603
        (git, "-C", str(repository), "credential", "fill"),
        input="protocol=https\nhost=github.com\n\n",
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "username=workflow-actor" in credentials
    assert "password=workflow-secret" in credentials


def test_prepare_existing_index_preserves_old_worktree_and_resumes(
    tmp_path: Path,
) -> None:
    """An existing remote index is resumed while local state remains recoverable."""

    repository, _remote = _create_repository_with_remote(tmp_path)
    index_dir = repository / "index"
    _git(repository, "branch", "index")
    _git(repository, "push", "-qu", "origin", "index")
    _git(repository, "worktree", "add", "-q", str(index_dir), "index")
    (index_dir / "recoverable.txt").write_text("retained\n", encoding="utf-8")
    messages: list[str] = []

    result = IndexWorkspacePreparer(
        GitRepository(repository),
        progress=messages.append,
    ).prepare("index", index_dir)

    assert not result.first_run
    assert _git(repository, "branch", "--show-current").stdout.strip() == "master"
    assert _git(index_dir, "branch", "--show-current").stdout.strip() == "index"
    assert (
        _git(
            index_dir.with_name("index.bak"),
            "branch",
            "--show-current",
        ).stdout.strip()
        == ""
    )
    assert (repository / "index.bak" / "recoverable.txt").read_text() == "retained\n"
    assert not (index_dir / "alpha").exists()
    assert GitRepository(index_dir).top_level_directory_count() == 2
    assert any("prepare-index-branch-ref" in message for message in messages)


def test_prepare_missing_index_creates_parentless_branch_without_source_switch(
    tmp_path: Path,
) -> None:
    """A new installation creates an empty index without mutating source state."""

    repository, _remote = _create_repository_with_remote(tmp_path)
    index_dir = repository / "index"
    source_head = _git(repository, "rev-parse", "HEAD").stdout.strip()

    result = IndexWorkspacePreparer(GitRepository(repository)).prepare(
        "index",
        index_dir,
    )

    assert result.first_run
    assert _git(repository, "branch", "--show-current").stdout.strip() == "master"
    assert _git(repository, "rev-parse", "HEAD").stdout.strip() == source_head
    index_commit = _git(
        repository,
        "rev-list",
        "--parents",
        "-n",
        "1",
        "refs/heads/index",
    )
    assert len(index_commit.stdout.split()) == 1
    assert (
        _git(
            repository,
            "ls-tree",
            "-r",
            "--name-only",
            "refs/remotes/origin/index",
        ).stdout
        == ""
    )
    assert _git(index_dir, "branch", "--show-current").stdout.strip() == "index"


def test_prepare_index_does_not_treat_remote_failure_as_missing_branch(
    tmp_path: Path,
) -> None:
    """A transport failure cannot trigger accidental index branch creation."""

    repository, _remote = _create_repository_with_remote(tmp_path)
    missing_remote = tmp_path / "missing.git"
    _git(repository, "remote", "set-url", "origin", str(missing_remote))
    index_dir = repository / "index"

    with pytest.raises(WorkspaceError, match="git ls-remote failed"):
        IndexWorkspacePreparer(GitRepository(repository)).prepare("index", index_dir)

    assert not index_dir.exists()
