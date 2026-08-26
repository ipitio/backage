"""Synchronize material upstream source changes into a deployment fork."""

from dataclasses import dataclass
from pathlib import Path

from .git import GitCommandRunner, WorkspaceError
from .merge_configuration import FORK_LOCAL_PATHS

_UPSTREAM_REMOTE = "bkg-upstream"
_HISTORY_DEPTHS = (128, 1_024)
_PARTIAL_CLONE_FILTER = "blob:none"


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

        fork_branch = self._validate_worktree(upstream_url, upstream_branch)
        self._configure_remote(upstream_url)
        upstream_tip = self._remote_tip(upstream_branch)
        fork_commit = self._run(
            ("rev-parse", "--verify", "HEAD^{commit}"),
            required=True,
        ).stdout.strip()
        if upstream_tip == fork_commit:
            return ForkSyncResult(False, upstream_tip)

        upstream_ref = f"refs/remotes/{_UPSTREAM_REMOTE}/{upstream_branch}"
        merge_base = self._fetch_comparable_history(
            fork_branch=fork_branch,
            upstream_branch=upstream_branch,
            upstream_ref=upstream_ref,
        )
        upstream_commit = self._run(
            ("rev-parse", "--verify", f"{upstream_ref}^{{commit}}"),
            required=True,
        ).stdout.strip()
        if self._is_ancestor(upstream_ref, "HEAD"):
            return ForkSyncResult(False, upstream_commit)

        if not merge_base:
            raise WorkspaceError("fork and upstream branches have no common commit")
        if not self._has_material_changes(merge_base, upstream_ref):
            return ForkSyncResult(False, upstream_commit)

        if self._is_ancestor("HEAD", upstream_ref):
            self._run(("merge", "--ff-only", upstream_ref), required=True)
        else:
            self._project_upstream(upstream_ref, upstream_commit)
        return ForkSyncResult(True, upstream_commit)

    def _validate_worktree(self, upstream_url: str, upstream_branch: str) -> str:
        if not self.is_worktree():
            raise WorkspaceError(f"not a Git worktree: {self.path.resolve()}")
        if not upstream_url.strip():
            raise WorkspaceError("upstream repository URL is required")
        branch = self._run(("check-ref-format", "--branch", upstream_branch))
        if branch.returncode != 0:
            raise WorkspaceError(f"invalid upstream branch: {upstream_branch}")
        fork_branch = self.current_branch()
        if not fork_branch:
            raise WorkspaceError("fork synchronization requires a checked-out branch")
        status = self._run(("status", "--porcelain"), required=True).stdout.strip()
        if status:
            raise WorkspaceError("fork synchronization requires a clean worktree")
        return fork_branch

    def _configure_remote(self, upstream_url: str) -> None:
        current = self._run(("remote", "get-url", _UPSTREAM_REMOTE))
        if current.returncode == 0:
            if current.stdout.strip() != upstream_url:
                self._run(
                    ("remote", "set-url", _UPSTREAM_REMOTE, upstream_url),
                    required=True,
                )
        else:
            remotes = self._run(("remote",), required=True).stdout.splitlines()
            if _UPSTREAM_REMOTE in remotes:
                raise WorkspaceError(f"could not read {_UPSTREAM_REMOTE} remote URL")
            self._run(
                ("remote", "add", _UPSTREAM_REMOTE, upstream_url),
                required=True,
            )
        self._run(
            ("config", f"remote.{_UPSTREAM_REMOTE}.promisor", "true"),
            required=True,
        )
        self._run(
            (
                "config",
                f"remote.{_UPSTREAM_REMOTE}.partialclonefilter",
                _PARTIAL_CLONE_FILTER,
            ),
            required=True,
        )

    def _remote_tip(self, upstream_branch: str) -> str:
        result = self._run(
            (
                "ls-remote",
                "--exit-code",
                _UPSTREAM_REMOTE,
                f"refs/heads/{upstream_branch}",
            ),
            required=True,
        )
        try:
            remote_tip, _ = result.stdout.split()
        except ValueError as error:
            raise WorkspaceError(
                f"could not resolve {_UPSTREAM_REMOTE}/{upstream_branch}"
            ) from error
        return remote_tip

    def _fetch_comparable_history(
        self,
        *,
        fork_branch: str,
        upstream_branch: str,
        upstream_ref: str,
    ) -> str:
        if not self._is_shallow_repository():
            self._fetch_branch(_UPSTREAM_REMOTE, upstream_branch, upstream_ref)
            return self._merge_base(upstream_ref)

        fork_remote, fork_remote_branch = self._fork_tracking_branch(fork_branch)
        fork_ref = f"refs/remotes/{fork_remote}/{fork_remote_branch}"
        for depth in _HISTORY_DEPTHS:
            self._fetch_branch(
                fork_remote,
                fork_remote_branch,
                fork_ref,
                depth=depth,
            )
            self._fetch_branch(
                _UPSTREAM_REMOTE,
                upstream_branch,
                upstream_ref,
                depth=depth,
            )
            if self._is_ancestor(upstream_ref, "HEAD"):
                return upstream_ref
            merge_base = self._merge_base(upstream_ref)
            if merge_base:
                return merge_base

        self._fetch_branch(
            fork_remote,
            fork_remote_branch,
            fork_ref,
            complete=True,
        )
        self._fetch_branch(
            _UPSTREAM_REMOTE,
            upstream_branch,
            upstream_ref,
            complete=True,
        )
        return self._merge_base(upstream_ref)

    def _fork_tracking_branch(self, fork_branch: str) -> tuple[str, str]:
        remote = self._run(("config", "--get", f"branch.{fork_branch}.remote"))
        remote_name = remote.stdout.strip() if remote.returncode == 0 else "origin"
        if not remote_name or remote_name == ".":
            remote_name = "origin"
        if self._run(("remote", "get-url", remote_name)).returncode != 0:
            raise WorkspaceError(
                "a shallow fork synchronization requires a tracking remote"
            )

        merge = self._run(("config", "--get", f"branch.{fork_branch}.merge"))
        merge_ref = merge.stdout.strip() if merge.returncode == 0 else ""
        prefix = "refs/heads/"
        remote_branch = (
            merge_ref.removeprefix(prefix)
            if merge_ref.startswith(prefix)
            else fork_branch
        )
        return remote_name, remote_branch

    def _fetch_branch(
        self,
        remote: str,
        branch: str,
        destination: str,
        *,
        depth: int | None = None,
        complete: bool = False,
    ) -> None:
        arguments = [
            "fetch",
            "--no-tags",
            f"--filter={_PARTIAL_CLONE_FILTER}",
        ]
        if depth is not None:
            arguments.append(f"--depth={depth}")
        elif complete and self._is_shallow_repository():
            arguments.append("--unshallow")
        arguments.extend(
            (
                remote,
                f"+refs/heads/{branch}:{destination}",
            )
        )
        self._run(tuple(arguments), required=True)

    def _is_shallow_repository(self) -> bool:
        result = self._run(
            ("rev-parse", "--is-shallow-repository"),
            required=True,
        )
        return result.stdout.strip() == "true"

    def _merge_base(self, upstream_ref: str) -> str:
        result = self._run(("merge-base", "HEAD", upstream_ref))
        if result.returncode not in {0, 1}:
            self._raise_command_error(("merge-base",), result)
        return result.stdout.strip()

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
