"""Publish generated index and source state to their owning Git branches."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ..files import atomic_text_output
from ..result import ExitStatus
from .git import GitCommandRunner, WorkspaceError
from .index import GitIndexRepository
from .source import GitSourceRepository

MessageSink = Callable[[str], None]
_SOURCE_PATHS = (":(top,glob)*.txt", "README.md")


def _discard_message(_message: str) -> None:
    return


@dataclass(frozen=True)
class WorkspacePublication:
    """Whether publication created commits on either owning branch."""

    index_committed: bool
    source_committed: bool


class GitBranchPublisher(GitCommandRunner):  # pylint: disable=too-few-public-methods
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


class UpdateWorkspacePublisher:  # pylint: disable=too-few-public-methods
    """Commit and push one completed update without mixing branch ownership."""

    def __init__(
        self,
        root: Path | GitSourceRepository,
        *,
        progress: MessageSink | None = None,
        commit_message: str | None = None,
    ) -> None:
        self.repository = (
            root if isinstance(root, GitSourceRepository) else GitSourceRepository(root)
        )
        self.root = self.repository.path.resolve()
        self.progress = progress or _discard_message
        self.commit_message = commit_message or datetime.now(UTC).date().isoformat()

    def publish(
        self,
        index_branch: str,
        index_dir: Path,
        state_file: Path,
    ) -> WorkspacePublication:
        """Publish index state first, then the source branch's generated files."""

        index_dir = index_dir.resolve()
        state_file = state_file.resolve()
        if index_dir == self.root or not index_dir.is_relative_to(self.root):
            raise WorkspaceError(
                f"index worktree must be inside repository root: {index_dir}"
            )
        if not self.repository.is_worktree():
            raise WorkspaceError(
                f"source repository is not a Git worktree: {self.root}"
            )
        if not state_file.is_file():
            raise WorkspaceError(f"runtime state file is missing: {state_file}")

        index_repository = GitIndexRepository(
            index_dir,
            environment=self.repository.command_environment,
            redacted_values=self.repository.redacted_values,
        )
        if not index_repository.is_worktree():
            raise WorkspaceError(f"index repository is not a Git worktree: {index_dir}")
        if index_repository.current_branch() != index_branch:
            raise WorkspaceError(
                f"index worktree is not on expected branch {index_branch}"
            )

        self._copy_state(state_file, index_dir / ".env")
        index_committed = GitBranchPublisher(
            index_dir,
            environment=self.repository.command_environment,
            redacted_values=self.repository.redacted_values,
        ).publish(
            index_branch,
            self.commit_message,
        )
        self._report_publication(index_committed, "index", index_branch)
        if index_committed:
            self._remove_published_backup(index_dir)

        source_branch = self.repository.current_branch()
        if not source_branch:
            raise WorkspaceError("source worktree is detached during publication")
        source_committed = GitBranchPublisher(
            self.root,
            environment=self.repository.command_environment,
            redacted_values=self.repository.redacted_values,
        ).publish(
            source_branch,
            self.commit_message,
            pathspecs=_SOURCE_PATHS,
            synchronize=True,
        )
        self._report_publication(source_committed, "source", source_branch)
        return WorkspacePublication(index_committed, source_committed)

    @staticmethod
    def _copy_state(source: Path, destination: Path) -> None:
        content = source.read_text(encoding="utf-8")
        with atomic_text_output(destination) as output:
            output.write(content)

    def _report_publication(self, committed: bool, label: str, branch: str) -> None:
        if not committed:
            self.progress(f"No {label} changes to commit")
        else:
            self.progress(f"Published {label} branch {branch}")

    def _remove_published_backup(self, index_dir: Path) -> None:
        backup = index_dir.with_name(f"{index_dir.name}.bak")
        if backup in self.repository.registered_worktree_paths():
            self.repository.remove_worktree(backup)


def published_run_status(run_status: int) -> ExitStatus:
    """Map a finalized run to the status consumed by release publication."""

    if run_status in (ExitStatus.SUCCESS, ExitStatus.GRACEFUL_STOP):
        return ExitStatus.SUCCESS
    return ExitStatus.NON_FATAL
