"""Git-backed workspace operations used during update setup."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
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


def clone_repository(
    source: str,
    destination: Path,
    branch: str,
) -> GitRepository:
    """Create a shallow single-branch source checkout."""

    git = resolve_executable("git")
    try:
        result = subprocess.run(  # noqa: S603
            (
                git,
                "-c",
                "credential.username=x-access-token",
                "-c",
                f"credential.helper={_CREDENTIAL_HELPER}",
                "clone",
                "--depth=1",
                "--branch",
                branch,
                "--single-branch",
                source,
                str(destination),
            ),
            check=False,
            capture_output=True,
            shell=False,
            text=True,
        )
    except OSError as error:
        raise WorkspaceError(f"could not start git clone: {error}") from error
    if result.returncode != 0:
        detail = _redact_git_detail(result.stderr.strip() or result.stdout.strip())
        message = f"git clone failed with status {result.returncode}"
        if detail:
            message = f"{message}: {detail}"
        raise WorkspaceError(message)
    return GitRepository(destination)


class _GitCommandRunner:  # pylint: disable=too-few-public-methods
    """Execute credential-safe Git commands for one worktree."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._git = resolve_executable("git")

    def _run(  # pylint: disable=too-many-arguments
        self,
        arguments: Sequence[str],
        *,
        environment: Mapping[str, str] | None = None,
        input_text: str | None = None,
        required: bool = False,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        try:
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
                env=(None if environment is None else {**os.environ, **environment}),
                input=input_text,
                shell=False,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as error:
            command = arguments[0] if arguments else "command"
            raise WorkspaceError(
                f"git {command} timed out after {timeout:g}s in {self.path}"
            ) from error
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


class GitRepository(_GitCommandRunner):
    """Run bounded Git workspace operations with structured arguments."""

    def is_worktree(self) -> bool:
        """Return whether the path belongs to a Git worktree."""

        result = self._run(("rev-parse", "--is-inside-work-tree"))
        return result.returncode == 0 and result.stdout.strip() == "true"

    def current_branch(self) -> str:
        """Return the current branch, or an empty value for detached HEAD."""

        result = self._run(("branch", "--show-current"), required=True)
        return result.stdout.strip()

    def remote_url(self, remote: str = "origin") -> str:
        """Return the configured URL for one remote."""

        return self._run(
            ("remote", "get-url", remote),
            required=True,
        ).stdout.strip()

    def latest_commit_epoch(self, revision: str) -> int | None:
        """Return a revision's latest commit time, or None when it is absent."""

        result = self._run(("log", "-1", "--format=%ct", revision))
        value = result.stdout.strip()
        if result.returncode != 0 or not value.isdecimal():
            return None
        return int(value)

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
        upstream_arguments = (
            "branch",
            f"--set-upstream-to={remote}/{branch}",
            branch,
        )
        upstream = self._run(upstream_arguments)
        if upstream.returncode == 0:
            return

        refspec = f"+refs/heads/{branch}:refs/remotes/{remote}/{branch}"
        self._run(
            ("config", "--local", "--add", f"remote.{remote}.fetch", refspec),
            required=True,
        )
        self._run(upstream_arguments, required=True)

    def create_empty_branch(self, branch: str) -> None:
        """Create a parentless empty-tree branch without changing the checkout."""

        tree = self._run(("mktree",), input_text="", required=True).stdout.strip()
        commit = self._run(
            ("commit-tree", tree, "-m", f"init {branch}"),
            required=True,
        ).stdout.strip()
        self._run(("update-ref", f"refs/heads/{branch}", commit), required=True)

    def push_branch(self, branch: str, remote: str = "origin") -> None:
        """Publish a local branch and configure its upstream."""

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

    def materialize_sparse_paths(
        self,
        paths: Iterable[str],
        *,
        replace: bool = False,
    ) -> None:
        """Materialize sparse paths, optionally replacing the completed paths."""

        if not self.is_worktree():
            return
        if replace:
            self._replace_sparse_paths(paths)
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

    def _replace_sparse_paths(self, paths: Iterable[str]) -> None:
        current = tuple(
            path
            for path in self._run(
                ("sparse-checkout", "list"),
                required=True,
            ).stdout.splitlines()
            if path
        )
        stageable = self._stageable_sparse_paths(current)
        if stageable:
            self._run(("add", "--all", "--", *stageable), required=True)
        selected = tuple(dict.fromkeys(path for path in paths if path))
        self._run(
            ("sparse-checkout", "set", "--skip-checks", "--stdin"),
            input_text="".join(f"{path}\n" for path in selected),
            required=True,
        )

    def _stageable_sparse_paths(self, paths: Sequence[str]) -> tuple[str, ...]:
        """Keep materialized or tracked paths and ignore absent sparse entries."""

        existing = tuple(path for path in paths if (self.path / path).exists())
        missing = tuple(path for path in paths if path not in existing)
        if not missing:
            return existing
        tracked = self._run(
            ("ls-files", "--", *missing),
            required=True,
        ).stdout.splitlines()
        tracked_missing: list[str] = []
        for path in missing:
            prefix = f"{path.rstrip('/')}/"
            if any(item == path or item.startswith(prefix) for item in tracked):
                tracked_missing.append(path)
        return existing + tuple(tracked_missing)

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


class GitControlRefRepository(_GitCommandRunner):
    """Read and mutate one exact remote control ref without checkout changes."""

    def remote_ref_sha(
        self,
        ref: str,
        *,
        remote: str = "origin",
        timeout: float | None = None,
    ) -> str | None:
        """Return one exact remote ref SHA, or None when the ref is absent."""

        result = self._run(
            ("ls-remote", "--refs", remote, ref),
            required=True,
            timeout=timeout,
        )
        first_line = next(iter(result.stdout.splitlines()), "")
        sha, _separator, _name = first_line.partition("\t")
        return sha or None

    def fetch_ref(
        self,
        ref: str,
        *,
        remote: str = "origin",
        timeout: float | None = None,
    ) -> str:
        """Fetch one exact ref and return the fetched commit SHA."""

        self._run(
            ("fetch", "--quiet", "--no-tags", "--depth=1", remote, ref),
            required=True,
            timeout=timeout,
        )
        return self._run(
            ("rev-parse", "FETCH_HEAD"),
            required=True,
        ).stdout.strip()

    def empty_tree(self) -> str:
        """Return Git's canonical empty-tree object ID."""

        return self._run(("mktree",), input_text="", required=True).stdout.strip()

    def commit_tree(
        self,
        message: str,
        *,
        parent: str | None = None,
        additional_message: str | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> str:
        """Create an empty-tree commit without changing the worktree."""

        arguments = ["commit-tree", self.empty_tree()]
        if parent is not None:
            arguments.extend(("-p", parent))
        arguments.extend(("-m", message))
        if additional_message is not None:
            arguments.extend(("-m", additional_message))
        return self._run(
            arguments,
            environment=environment,
            required=True,
        ).stdout.strip()

    def commit_tree_id(self, commit: str) -> str:
        """Return the tree object ID referenced by a commit."""

        return self._run(
            ("show", "-s", "--format=%T", commit),
            required=True,
        ).stdout.strip()

    def commit_message(self, commit: str) -> str:
        """Return a commit's complete message."""

        return self._run(
            ("show", "-s", "--format=%B", commit),
            required=True,
        ).stdout

    def push_ref(
        self,
        commit: str,
        ref: str,
        *,
        remote: str = "origin",
        force_with_lease: str | None = None,
    ) -> bool:
        """Try to push one commit to a ref, returning False for a rejected race."""

        arguments = ["push", "--quiet"]
        if force_with_lease is not None:
            arguments.append(f"--force-with-lease={ref}:{force_with_lease}")
        arguments.extend((remote, f"{commit}:{ref}"))
        return self._run(arguments).returncode == 0


class GitBranchPublisher(_GitCommandRunner):  # pylint: disable=too-few-public-methods
    """Synchronize, commit, and push one branch-owned set of paths."""

    def publish(
        self,
        branch: str,
        message: str,
        *,
        pathspecs: Sequence[str] | None = None,
        synchronize: bool = False,
    ) -> bool:
        """Publish staged branch paths and return whether a commit was created."""

        if not message:
            raise WorkspaceError("Git commit message is required")
        if synchronize:
            self._run(
                ("pull", "--rebase", "--autostash"),
                required=True,
            )
        current = self._run(
            ("branch", "--show-current"),
            required=True,
        ).stdout.strip()
        if current != branch:
            raise WorkspaceError(
                f"worktree is on branch {current or '<detached>'}, expected {branch}"
            )

        add_arguments = (
            ("add", "--all", "--", ".")
            if pathspecs is None
            else ("add", "--all", "--", *pathspecs)
        )
        self._run(add_arguments, required=True)
        changed = self._run(("diff", "--cached", "--quiet", "--exit-code"))
        if changed.returncode == 0:
            return False
        if changed.returncode != 1:
            self._raise_command_error(
                ("diff", "--cached"),
                changed,
            )

        self._run(
            ("commit", "-m", message),
            required=True,
        )
        self._run(
            (
                "push",
                "--set-upstream",
                "origin",
                f"refs/heads/{branch}:refs/heads/{branch}",
            ),
            required=True,
        )
        return True


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
