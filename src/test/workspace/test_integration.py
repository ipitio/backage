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
    UpdateWorkspacePublisher,
    WorkspaceError,
    WorkspaceLayout,
    import_workflow_payload,
    published_run_status,
)
from bkg_py.workspace.repository import ensure_pages_root

from .repository_support import (
    create_repository as _create_repository,
)
from .repository_support import (
    create_repository_with_remote as _create_repository_with_remote,
)
from .repository_support import (
    git as _git,
)


def _create_publication_workspace(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    repository, remote = _create_repository_with_remote(tmp_path)
    (repository / "owners.txt").write_text("original-owner\n", encoding="utf-8")
    (repository / "nested").mkdir()
    (repository / "nested" / "notes.txt").write_text(
        "original notes\n",
        encoding="utf-8",
    )
    (repository / "application.py").write_text("original = True\n", encoding="utf-8")
    _git(repository, "add", "-A")
    _git(repository, "commit", "-qm", "source files")
    _git(repository, "push", "-q", "origin", "master")
    _git(repository, "branch", "index")
    _git(repository, "push", "-qu", "origin", "index")

    index_dir = repository / "index"
    _git(repository, "worktree", "add", "-q", str(index_dir), "index")
    (index_dir / "recoverable.txt").write_text("old state\n", encoding="utf-8")
    IndexWorkspacePreparer(GitRepository(repository)).prepare("index", index_dir)
    state_file = repository / "src" / "env.env"
    state_file.parent.mkdir()
    state_file.write_text("BKG_TIMEOUT=1\n", encoding="utf-8")
    return repository, remote, index_dir, state_file


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

    repository.materialize_sparse_paths(("alpha", "beta"))

    assert (path / "alpha" / "repo-a" / "package.json").is_file()
    assert (path / "beta" / "repo-b" / "package.json").is_file()


def test_sparse_repository_stages_completed_paths_before_replacing_them(
    tmp_path: Path,
) -> None:
    """Only the active owner wave stays hydrated without losing prior changes."""

    path = tmp_path / "index"
    _create_repository(path)
    repository = GitRepository(path)
    repository.set_sparse_root()
    repository.materialize_sparse_paths(("alpha",), replace=True)
    package = path / "alpha" / "repo-a" / "package.json"
    package.write_text('{"updated":true}\n', encoding="utf-8")

    repository.materialize_sparse_paths(("beta",), replace=True)

    assert not (path / "alpha").exists()
    assert (path / "beta" / "repo-b" / "package.json").is_file()
    assert _git(path, "diff", "--cached", "--name-only").stdout.splitlines() == [
        "alpha/repo-a/package.json"
    ]


def test_sparse_repository_ignores_an_absent_path_when_replacing_it(
    tmp_path: Path,
) -> None:
    """A queued owner without an index tree does not block the next sparse wave."""

    path = tmp_path / "index"
    _create_repository(path)
    repository = GitRepository(path)
    repository.set_sparse_root()
    repository.materialize_sparse_paths(("missing-owner",), replace=True)

    repository.materialize_sparse_paths(("beta",), replace=True)

    assert not (path / "missing-owner").exists()
    assert (path / "beta" / "repo-b" / "package.json").is_file()


def test_sparse_operations_ignore_non_repository_path(tmp_path: Path) -> None:
    """Optional local index paths retain the previous no-op behavior."""

    repository = GitRepository(tmp_path / "missing")

    assert not repository.is_worktree()
    repository.set_sparse_root()
    repository.materialize_sparse_paths(("alpha",))
    repository.materialize_sparse_paths(("alpha",), replace=True)
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


def test_prepare_existing_index_from_single_branch_clone_configures_tracking(
    tmp_path: Path,
) -> None:
    """A workflow-style clone can track an explicitly fetched index branch."""

    source, remote = _create_repository_with_remote(tmp_path)
    _git(source, "branch", "index")
    _git(source, "push", "-q", "origin", "index")
    repository = tmp_path / "clone"
    git = shutil.which("git")
    assert git is not None
    subprocess.run(  # noqa: S603
        (
            git,
            "clone",
            "-q",
            "--depth=1",
            "--single-branch",
            "--branch",
            "master",
            remote.as_uri(),
            str(repository),
        ),
        check=True,
    )
    index_dir = repository / "index"

    result = IndexWorkspacePreparer(GitRepository(repository)).prepare(
        "index",
        index_dir,
    )

    assert not result.first_run
    upstream = _git(
        index_dir, "rev-parse", "--abbrev-ref", "@{upstream}"
    ).stdout.strip()
    assert upstream == "origin/index"
    fetch_refspecs = _git(
        repository,
        "config",
        "--get-all",
        "remote.origin.fetch",
    ).stdout.splitlines()
    assert fetch_refspecs == [
        "+refs/heads/master:refs/remotes/origin/master",
        "+refs/heads/index:refs/remotes/origin/index",
    ]


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


def test_update_publication_keeps_branch_ownership_and_skips_no_op_commits(
    tmp_path: Path,
) -> None:
    """Generated index state and selected source files reach only their branches."""

    repository, remote, index_dir, state_file = _create_publication_workspace(tmp_path)
    GitRepository(index_dir).materialize_sparse_paths(("gamma",))
    generated = index_dir / "gamma" / "repo" / "package.json"
    generated.parent.mkdir(parents=True)
    generated.write_text("{}\n", encoding="utf-8")
    (repository / "owners.txt").write_text("next-owner\n", encoding="utf-8")
    (repository / "README.md").write_text("updated source\n", encoding="utf-8")
    (repository / "nested" / "notes.txt").write_text(
        "local notes\n",
        encoding="utf-8",
    )
    (repository / "application.py").write_text("local = True\n", encoding="utf-8")
    publisher = UpdateWorkspacePublisher(
        repository,
        commit_message="2026-07-21",
    )

    result = publisher.publish("index", index_dir, state_file)

    assert result.index_committed
    assert result.source_committed
    assert not (repository / "index.bak").exists()
    assert _git(remote, "show", "index:.env").stdout == "BKG_TIMEOUT=1\n"
    assert _git(remote, "show", "index:gamma/repo/package.json").stdout == "{}\n"
    assert _git(remote, "show", "master:owners.txt").stdout == "next-owner\n"
    assert _git(remote, "show", "master:README.md").stdout == "updated source\n"
    assert _git(remote, "show", "master:nested/notes.txt").stdout == "original notes\n"
    assert _git(remote, "show", "master:application.py").stdout == "original = True\n"

    index_head = _git(index_dir, "rev-parse", "HEAD").stdout.strip()
    source_head = _git(repository, "rev-parse", "HEAD").stdout.strip()
    second = publisher.publish("index", index_dir, state_file)

    assert not second.index_committed
    assert not second.source_committed
    assert _git(index_dir, "rev-parse", "HEAD").stdout.strip() == index_head
    assert _git(repository, "rev-parse", "HEAD").stdout.strip() == source_head
    assert (
        main(
            [
                "workspace",
                "publish-update",
                str(repository),
                "index",
                str(index_dir),
                str(state_file),
                str(ExitStatus.GRACEFUL_STOP),
            ]
        )
        == ExitStatus.SUCCESS
    )


def test_update_publication_retains_index_commit_when_push_fails(
    tmp_path: Path,
) -> None:
    """A failed push leaves the completed index commit and worktree available."""

    repository, _remote, index_dir, state_file = _create_publication_workspace(tmp_path)
    GitRepository(index_dir).materialize_sparse_paths(("gamma",))
    generated = index_dir / "gamma" / "repo" / "package.json"
    generated.parent.mkdir(parents=True)
    generated.write_text("{}\n", encoding="utf-8")
    _git(repository, "remote", "set-url", "origin", str(tmp_path / "missing.git"))

    with pytest.raises(WorkspaceError, match="git push failed"):
        UpdateWorkspacePublisher(
            repository,
            commit_message="2026-07-21",
        ).publish("index", index_dir, state_file)

    assert generated.is_file()
    assert (repository / "index.bak" / "recoverable.txt").is_file()
    assert _git(index_dir, "status", "--short").stdout == ""
    assert _git(index_dir, "rev-list", "--count", "origin/index..HEAD").stdout == "1\n"


def test_publish_update_cli_skips_failed_run_state(tmp_path: Path) -> None:
    """A run that did not finalize cannot publish state ahead of its database."""

    repository, remote, index_dir, state_file = _create_publication_workspace(tmp_path)
    state_file.write_text("BKG_TIMEOUT=failed\n", encoding="utf-8")
    index_head = _git(index_dir, "rev-parse", "HEAD").stdout.strip()

    status = main(
        [
            "workspace",
            "publish-update",
            str(repository),
            "index",
            str(index_dir),
            str(state_file),
            str(ExitStatus.NON_FATAL),
        ]
    )

    assert status == ExitStatus.NON_FATAL
    assert _git(index_dir, "rev-parse", "HEAD").stdout.strip() == index_head
    assert _git(remote, "show", "index:.env").stdout == "state\n"
    assert _git(remote, "show", "master:owners.txt").stdout == "original-owner\n"


@pytest.mark.parametrize(
    ("run_status", "expected"),
    [
        (ExitStatus.SUCCESS, ExitStatus.SUCCESS),
        (ExitStatus.GRACEFUL_STOP, ExitStatus.SUCCESS),
        (ExitStatus.NON_FATAL, ExitStatus.NON_FATAL),
        (ExitStatus.FAILURE, ExitStatus.NON_FATAL),
        (99, ExitStatus.NON_FATAL),
    ],
)
def test_published_run_status_preserves_release_safety(
    run_status: int,
    expected: ExitStatus,
) -> None:
    """Only successful or gracefully finalized runs may publish a release."""

    assert published_run_status(run_status) is expected
