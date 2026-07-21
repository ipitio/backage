"""Local Git repository fixtures shared by workspace integration tests."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


def git(
    repository: Path,
    *arguments: str,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run a required Git command in one test repository."""

    executable = shutil.which("git")
    if executable is None:
        raise RuntimeError("Git is required for workspace tests")
    return subprocess.run(  # noqa: S603
        (executable, "-C", str(repository), *arguments),
        check=check,
        capture_output=True,
        text=True,
    )


def create_repository(path: Path) -> None:
    """Create a source repository with representative index content."""

    path.mkdir()
    executable = shutil.which("git")
    if executable is None:
        raise RuntimeError("Git is required for workspace tests")
    subprocess.run(  # noqa: S603
        (executable, "init", "-q", "-b", "master", str(path)),
        check=True,
    )
    git(path, "config", "user.name", "test")
    git(path, "config", "user.email", "test@example.com")
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
    git(path, "add", "-A")
    git(path, "commit", "-qm", "init")


def create_repository_with_remote(tmp_path: Path) -> tuple[Path, Path]:
    """Create a source repository and matching local bare origin."""

    remote = tmp_path / "remote.git"
    remote.mkdir()
    git(remote, "init", "--bare", "-q", "--initial-branch=master")
    repository = tmp_path / "repository"
    create_repository(repository)
    git(repository, "remote", "add", "origin", str(remote))
    git(repository, "push", "-qu", "origin", "master")
    return repository, remote


def clone_repository(source: Path, destination: Path) -> None:
    """Clone one local repository into a test worktree."""

    executable = shutil.which("git")
    if executable is None:
        raise RuntimeError("Git is required for workspace tests")
    subprocess.run(  # noqa: S603
        (executable, "clone", "--quiet", str(source), str(destination)),
        check=True,
    )
