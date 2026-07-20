"""Git-backed workspace operations used during update setup."""

from __future__ import annotations

import subprocess
from collections.abc import Iterable, Sequence
from pathlib import Path

from ..files import atomic_text_output
from ..runtime import resolve_executable

_SPARSE_PATH_BATCH_SIZE = 100


class WorkspaceError(RuntimeError):
    """A repository workspace operation could not be completed."""


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
        required: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(  # noqa: S603
            (self._git, "-C", str(self.path), *arguments),
            check=False,
            capture_output=True,
            shell=False,
            text=True,
        )
        if required and result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            message = (
                f"git {arguments[0]} failed with status {result.returncode} "
                f"in {self.path}"
            )
            if detail:
                message = f"{message}: {detail}"
            raise WorkspaceError(message)
        return result


def ensure_pages_root(path: Path) -> None:
    """Create the Pages root and atomically publish an empty `.nojekyll`."""

    path.mkdir(parents=True, exist_ok=True)
    with atomic_text_output(path / ".nojekyll"):
        pass
