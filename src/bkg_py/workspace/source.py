"""Source-checkout Git operations used during update setup."""

import subprocess
from collections.abc import Iterable, Mapping
from pathlib import Path

from ..runtime import resolve_executable
from .git import (
    CREDENTIAL_HELPER,
    GitCommandRunner,
    WorkspaceError,
    redact_git_detail,
)
from .settings import GitIdentity

_MISSING_REMOTE_REF_STATUS = 2


class GitSourceRepository(GitCommandRunner):
    """Manage the source branch and its linked worktrees."""

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

    def configure_for_updates(self, identity: GitIdentity) -> None:
        """Configure commit identity, credentials, and large-worktree settings."""

        if not identity.name:
            raise WorkspaceError("GITHUB_ACTOR is required for Git configuration")
        settings = (
            ("user.name", identity.name),
            ("user.email", identity.email),
            ("credential.username", identity.name),
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
            ("config", "--local", "--add", "credential.helper", CREDENTIAL_HELPER),
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


def clone_repository(
    source: str,
    destination: Path,
    branch: str,
    *,
    environment: Mapping[str, str] | None = None,
    redacted_values: Iterable[str] = (),
) -> GitSourceRepository:
    """Create a shallow single-branch source checkout."""

    git = resolve_executable("git")
    try:
        result = subprocess.run(  # noqa: S603
            (
                git,
                "-c",
                "credential.username=x-access-token",
                "-c",
                f"credential.helper={CREDENTIAL_HELPER}",
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
            env=None if environment is None else dict(environment),
            shell=False,
            text=True,
        )
    except OSError as error:
        raise WorkspaceError(f"could not start git clone: {error}") from error
    if result.returncode != 0:
        detail = redact_git_detail(
            result.stderr.strip() or result.stdout.strip(),
            redacted_values,
        )
        message = f"git clone failed with status {result.returncode}"
        if detail:
            message = f"{message}: {detail}"
        raise WorkspaceError(message)
    return GitSourceRepository(
        destination,
        environment=environment,
        redacted_values=redacted_values,
    )
