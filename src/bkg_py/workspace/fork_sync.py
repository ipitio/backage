"""Synchronize material upstream source changes into a deployment fork."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .git import GitCommandRunner, WorkspaceError
from .merge_configuration import FORK_LOCAL_PATHS

_UPSTREAM_REMOTE = "bkg-upstream"


@dataclass(frozen=True)
class ForkSyncResult:
    """Outcome of comparing and optionally merging one upstream branch."""

    updated: bool
    upstream_commit: str


class ForkSourceSynchronizer(GitCommandRunner):
    """Project upstream source while retaining deployment-owned branch files."""

    def synchronize(
        self,
        *,
        upstream_url: str,
        upstream_branch: str,
    ) -> ForkSyncResult:
        """Fetch and project material upstream changes into the current branch."""

        self._validate_worktree(upstream_url, upstream_branch)
        self._configure_remote(upstream_url)
        upstream_ref = f"refs/remotes/{_UPSTREAM_REMOTE}/{upstream_branch}"
        self._run(
            (
                "fetch",
                "--no-tags",
                _UPSTREAM_REMOTE,
                f"+refs/heads/{upstream_branch}:{upstream_ref}",
            ),
            required=True,
        )
        upstream_commit = self._run(
            ("rev-parse", "--verify", f"{upstream_ref}^{{commit}}"),
            required=True,
        ).stdout.strip()
        if self._is_ancestor(upstream_ref, "HEAD"):
            return ForkSyncResult(False, upstream_commit)

        merge_base = self._run(
            ("merge-base", "HEAD", upstream_ref),
            required=True,
        ).stdout.strip()
        if not merge_base:
            raise WorkspaceError("fork and upstream branches have no common commit")
        if not self._has_material_changes(merge_base, upstream_ref):
            return ForkSyncResult(False, upstream_commit)

        if self._is_ancestor("HEAD", upstream_ref):
            self._run(("merge", "--ff-only", upstream_ref), required=True)
        else:
            self._project_upstream(upstream_ref, upstream_commit)
        return ForkSyncResult(True, upstream_commit)

    def _validate_worktree(self, upstream_url: str, upstream_branch: str) -> None:
        if not self.is_worktree():
            raise WorkspaceError(f"not a Git worktree: {self.path.resolve()}")
        if not upstream_url.strip():
            raise WorkspaceError("upstream repository URL is required")
        branch = self._run(("check-ref-format", "--branch", upstream_branch))
        if branch.returncode != 0:
            raise WorkspaceError(f"invalid upstream branch: {upstream_branch}")
        if not self.current_branch():
            raise WorkspaceError("fork synchronization requires a checked-out branch")
        status = self._run(("status", "--porcelain"), required=True).stdout.strip()
        if status:
            raise WorkspaceError("fork synchronization requires a clean worktree")

    def _configure_remote(self, upstream_url: str) -> None:
        current = self._run(("remote", "get-url", _UPSTREAM_REMOTE))
        if current.returncode == 0:
            if current.stdout.strip() != upstream_url:
                self._run(
                    ("remote", "set-url", _UPSTREAM_REMOTE, upstream_url),
                    required=True,
                )
            return
        remotes = self._run(("remote",), required=True).stdout.splitlines()
        if _UPSTREAM_REMOTE in remotes:
            raise WorkspaceError(f"could not read {_UPSTREAM_REMOTE} remote URL")
        self._run(("remote", "add", _UPSTREAM_REMOTE, upstream_url), required=True)

    def _is_ancestor(self, ancestor: str, descendant: str) -> bool:
        result = self._run(("merge-base", "--is-ancestor", ancestor, descendant))
        if result.returncode not in {0, 1}:
            self._raise_command_error(("merge-base",), result)
        return result.returncode == 0

    def _has_material_changes(self, merge_base: str, upstream_ref: str) -> bool:
        excluded = tuple(f":(exclude){path}" for path in FORK_LOCAL_PATHS)
        result = self._run(
            (
                "diff",
                "--quiet",
                f"{merge_base}..{upstream_ref}",
                "--",
                ".",
                *excluded,
            )
        )
        if result.returncode not in {0, 1}:
            self._raise_command_error(("diff",), result)
        return result.returncode == 1

    def _project_upstream(self, upstream_ref: str, upstream_commit: str) -> None:
        fork_commit = self._run(
            ("rev-parse", "--verify", "HEAD^{commit}"),
            required=True,
        ).stdout.strip()
        try:
            self._run(("read-tree", "--reset", "-u", upstream_ref), required=True)
            for path in FORK_LOCAL_PATHS:
                local_path = self._run(("cat-file", "-e", f"{fork_commit}:{path}"))
                if local_path.returncode == 0:
                    self._run(
                        (
                            "restore",
                            f"--source={fork_commit}",
                            "--staged",
                            "--worktree",
                            "--",
                            path,
                        ),
                        required=True,
                    )
            tree = self._run(("write-tree",), required=True).stdout.strip()
            merge_commit = self._run(
                (
                    "commit-tree",
                    tree,
                    "-p",
                    fork_commit,
                    "-p",
                    upstream_commit,
                    "-m",
                    f"Merge upstream source {upstream_commit[:12]}",
                ),
                required=True,
            ).stdout.strip()
            self._run(("reset", "--hard", merge_commit), required=True)
        except WorkspaceError:
            self._run(("reset", "--hard", fork_commit))
            raise


def synchronize_fork_source(
    repository_path: Path,
    *,
    upstream_url: str,
    upstream_branch: str,
) -> ForkSyncResult:
    """Synchronize one clean fork worktree with its upstream source branch."""

    return ForkSourceSynchronizer(repository_path).synchronize(
        upstream_url=upstream_url,
        upstream_branch=upstream_branch,
    )
