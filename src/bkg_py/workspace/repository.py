"""Git-backed workspace operations used during update setup."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from ..files import atomic_text_output
from ..runtime import resolve_executable

_SPARSE_PATH_BATCH_SIZE = 100
_MISSING_REMOTE_REF_STATUS = 2
_CREDENTIAL_HELPER = "!f() { printf '%s\\n' \"password=$GITHUB_TOKEN\"; }; f"
_URL_CREDENTIALS = re.compile(r"(https?://)[^/@\s]+@")
MessageSink = Callable[[str], None]


def _discard_message(_message: str) -> None:
    return


def _redact_git_detail(detail: str) -> str:
    token = os.environ.get("GITHUB_TOKEN", "")
    if token:
        detail = detail.replace(token, "***")
    return _URL_CREDENTIALS.sub(r"\1***@", detail)


class WorkspaceError(RuntimeError):
    """A repository workspace operation could not be completed."""


@dataclass(frozen=True)
class IndexWorkspacePreparation:
    """Outcome of preparing an index branch and linked worktree."""

    first_run: bool


class GitRepository:
    """Run bounded Git workspace operations with structured arguments."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._git = resolve_executable("git")

    def is_worktree(self) -> bool:
        """Return whether the path belongs to a Git worktree."""

        result = self._run(("rev-parse", "--is-inside-work-tree"))
        return result.returncode == 0 and result.stdout.strip() == "true"

    def current_branch(self) -> str:
        """Return the current branch, or an empty value for detached HEAD."""

        result = self._run(("branch", "--show-current"), required=True)
        return result.stdout.strip()

    def configure_for_updates(self, actor: str) -> None:
        """Configure commit identity, credentials, and large-worktree settings."""

        if not actor:
            raise WorkspaceError("GITHUB_ACTOR is required for Git configuration")
        settings = (
            ("user.name", actor),
            ("user.email", f"{actor}@users.noreply.github.com"),
            ("credential.username", actor),
            ("core.sharedRepository", "all"),
            ("remote.origin.promisor", "true"),
            ("remote.origin.partialclonefilter", "blob:none"),
            ("extensions.partialClone", "origin"),
            ("core.untrackedcache", "true"),
            ("feature.manyFiles", "true"),
        )
        for key, value in settings:
            self._run(("config", "--local", key, value), required=True)
        self._run(
            ("config", "--local", "--replace-all", "credential.helper", ""),
            required=True,
        )
        self._run(
            ("config", "--local", "--add", "credential.helper", _CREDENTIAL_HELPER),
            required=True,
        )

        fsmonitor = self._run(("fsmonitor--daemon", "status"))
        if fsmonitor.returncode != 0:
            fsmonitor = self._run(("fsmonitor--daemon", "start"))
        if fsmonitor.returncode == 0:
            self._run(("config", "--local", "core.fsmonitor", "true"), required=True)
        else:
            self._run(("config", "--local", "--unset-all", "core.fsmonitor"))
        self._run(("update-index", "--index-version", "4"), required=True)

    def validate_branch_name(self, branch: str) -> None:
        """Reject a value that cannot identify a local branch."""

        self._run(("check-ref-format", "--branch", branch), required=True)

    def remote_branch_exists(self, branch: str, remote: str = "origin") -> bool:
        """Check a remote branch while distinguishing absence from transport failure."""

        result = self._run(
            (
                "ls-remote",
                "--exit-code",
                "--heads",
                remote,
                f"refs/heads/{branch}",
            )
        )
        if result.returncode == 0:
            return True
        if result.returncode == _MISSING_REMOTE_REF_STATUS:
            return False
        self._raise_command_error(("ls-remote", remote, branch), result)
        raise AssertionError("unreachable")

    def fetch_remote_branch(self, branch: str, remote: str = "origin") -> None:
        """Fetch one remote branch into its remote-tracking ref."""

        self._run(
            (
                "fetch",
                "--depth=1",
                "--filter=blob:none",
                remote,
                f"+refs/heads/{branch}:refs/remotes/{remote}/{branch}",
            ),
            required=True,
        )

    def reset_local_branch(self, branch: str, remote: str = "origin") -> None:
        """Point a local branch at its fetched remote-tracking ref."""

        self._run(
            ("branch", "--force", branch, f"refs/remotes/{remote}/{branch}"),
            required=True,
        )
        self._run(
            ("branch", f"--set-upstream-to={remote}/{branch}", branch),
            required=True,
        )

    def create_empty_branch(self, branch: str) -> None:
        """Create a parentless empty-tree branch without changing the checkout."""

        tree = self._run(("mktree",), input_text="", required=True).stdout.strip()
        commit = self._run(
            ("commit-tree", tree, "-m", f"init {branch}"),
            required=True,
        ).stdout.strip()
        self._run(("update-ref", f"refs/heads/{branch}", commit), required=True)

    def push_branch(self, branch: str, remote: str = "origin") -> None:
        """Publish a newly created local branch and configure its upstream."""

        self._run(
            (
                "push",
                "--set-upstream",
                remote,
                f"refs/heads/{branch}:refs/heads/{branch}",
            ),
            required=True,
        )

    def registered_worktree_paths(self) -> frozenset[Path]:
        """Return absolute paths registered as worktrees for this repository."""

        result = self._run(("worktree", "list", "--porcelain", "-z"), required=True)
        return frozenset(
            Path(field.removeprefix("worktree ")).resolve()
            for field in result.stdout.split("\0")
            if field.startswith("worktree ")
        )

    def remove_worktree(self, path: Path) -> None:
        """Remove one registered worktree and its administrative state."""

        self._run(("worktree", "remove", "--force", str(path)), required=True)

    def move_worktree(self, source: Path, destination: Path) -> None:
        """Move a registered worktree while retaining its files."""

        destination.parent.mkdir(parents=True, exist_ok=True)
        self._run(
            ("worktree", "move", "--force", str(source), str(destination)),
            required=True,
        )

    def add_worktree(self, path: Path, branch: str) -> None:
        """Attach a local branch without materializing its complete tree."""

        path.parent.mkdir(parents=True, exist_ok=True)
        self._run(
            ("worktree", "add", "--no-checkout", "--force", str(path), branch),
            required=True,
        )

    def detach(self) -> None:
        """Detach the current worktree from its local branch."""

        self._run(("switch", "--detach"), required=True)

    def reset_hard(self, revision: str) -> None:
        """Reset the current worktree to a known fetched revision."""

        self._run(("reset", "--hard", revision), required=True)

    def set_sparse_root(self) -> None:
        """Materialize only root files in a cone-mode sparse checkout."""

        if not self.is_worktree():
            return
        self._run(("sparse-checkout", "init", "--cone"), required=True)
        self._run(("sparse-checkout", "set"), required=True)

    def add_sparse_paths(self, paths: Iterable[str]) -> None:
        """Materialize sparse paths in bounded Git command batches."""

        if not self.is_worktree():
            return
        batch: list[str] = []
        for path in paths:
            if not path:
                continue
            batch.append(path)
            if len(batch) >= _SPARSE_PATH_BATCH_SIZE:
                self._add_sparse_batch(batch)
                batch = []
        if batch:
            self._add_sparse_batch(batch)

    def top_level_directory_count(self) -> int:
        """Count tracked top-level directories without materializing them."""

        if not self.is_worktree():
            return 0
        result = self._run(
            ("ls-tree", "-d", "--name-only", "HEAD"),
            required=True,
        )
        return sum(bool(line) for line in result.stdout.splitlines())

    def _add_sparse_batch(self, paths: Sequence[str]) -> None:
        self._run(
            ("sparse-checkout", "add", "--skip-checks", "--", *paths),
            required=True,
        )

    def _run(
        self,
        arguments: Sequence[str],
        *,
        input_text: str | None = None,
        required: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(  # noqa: S603
            (
                self._git,
                "-c",
                f"safe.directory={self.path.resolve()}",
                "-C",
                str(self.path),
                *arguments,
            ),
            check=False,
            capture_output=True,
            input=input_text,
            shell=False,
            text=True,
        )
        if required and result.returncode != 0:
            self._raise_command_error(arguments, result)
        return result

    def _raise_command_error(
        self,
        arguments: Sequence[str],
        result: subprocess.CompletedProcess[str],
    ) -> None:
        detail = _redact_git_detail(result.stderr.strip() or result.stdout.strip())
        message = (
            f"git {arguments[0]} failed with status {result.returncode} in {self.path}"
        )
        if detail:
            message = f"{message}: {detail}"
        raise WorkspaceError(message)


class IndexWorkspacePreparer:  # pylint: disable=too-few-public-methods
    """Prepare the remote index branch and its sparse linked worktree."""

    def __init__(
        self,
        repository: GitRepository,
        *,
        progress: MessageSink | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.repository = repository
        self.progress = progress or _discard_message
        self.clock = clock

    def prepare(
        self,
        index_branch: str,
        index_dir: Path,
    ) -> IndexWorkspacePreparation:
        """Prepare one index branch and return whether this is its first run."""

        index_dir = index_dir.resolve()
        self._validate_index_path(index_dir)
        self.repository.validate_branch_name(index_branch)

        started_at = self.clock()
        branch_exists = self.repository.remote_branch_exists(index_branch)
        self._log_phase("check-index-branch", started_at)

        self._preserve_current_worktree(index_dir)
        started_at = self.clock()
        if branch_exists:
            self.repository.fetch_remote_branch(index_branch)
            self.repository.reset_local_branch(index_branch)
            self._log_phase("prepare-index-branch-ref", started_at)
        else:
            self.repository.create_empty_branch(index_branch)
            self.repository.push_branch(index_branch)
            self.repository.fetch_remote_branch(index_branch)
            self.repository.reset_local_branch(index_branch)
            self._log_phase("create-index-branch", started_at)

        started_at = self.clock()
        self.repository.add_worktree(index_dir, index_branch)
        self._log_phase("attach-index-worktree", started_at)

        started_at = self.clock()
        index_repository = GitRepository(index_dir)
        index_repository.set_sparse_root()
        index_repository.reset_hard(f"refs/remotes/origin/{index_branch}")
        self._log_phase("prepare-index-worktree", started_at)
        return IndexWorkspacePreparation(first_run=not branch_exists)

    def _validate_index_path(self, index_dir: Path) -> None:
        root = self.repository.path.resolve()
        if index_dir == root or not index_dir.is_relative_to(root):
            raise WorkspaceError(
                f"index worktree must be inside repository root: {index_dir}"
            )

    def _preserve_current_worktree(self, index_dir: Path) -> None:
        registered = self.repository.registered_worktree_paths()
        if not index_dir.exists() and not index_dir.is_symlink():
            if index_dir in registered:
                self.repository.remove_worktree(index_dir)
            return

        backup = index_dir.with_name(f"{index_dir.name}.bak")
        self._remove_backup(backup, registered)
        if index_dir in registered:
            self.repository.move_worktree(index_dir, backup)
            GitRepository(backup).detach()
            return
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(index_dir), str(backup))

    def _remove_backup(self, backup: Path, registered: frozenset[Path]) -> None:
        if backup in registered:
            self.repository.remove_worktree(backup)
        if backup.is_symlink() or backup.is_file():
            backup.unlink(missing_ok=True)
        elif backup.exists():
            shutil.rmtree(backup)

    def _log_phase(self, phase: str, started_at: float) -> None:
        elapsed = max(0, int(self.clock() - started_at))
        self.progress(f"Update setup phase '{phase}' completed in {elapsed}s")


def ensure_pages_root(path: Path) -> None:
    """Create the Pages root and atomically publish an empty `.nojekyll`."""

    path.mkdir(parents=True, exist_ok=True)
    with atomic_text_output(path / ".nojekyll"):
        pass
