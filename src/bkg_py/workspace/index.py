"""Index-branch preparation, sparse checkout, and catalog operations."""

import shutil
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from ..database.models import PackageCatalogPath
from ..files import atomic_text_output
from ..publication.site_shell import SITE_CONTENT_DIRECTORY
from .git import GitCommandRunner, WorkspaceError
from .source import GitSourceRepository

_SPARSE_PATH_BATCH_SIZE = 100
_PERSISTENT_SPARSE_PATHS = (SITE_CONTENT_DIRECTORY,)
_PACKAGE_PATH_PARTS = 3
_PACKAGE_FILENAME_INDEX = 2
MessageSink = Callable[[str], None]


def _discard_message(_message: str) -> None:
    return


@dataclass(frozen=True)
class IndexWorkspacePreparation:
    """Outcome of preparing an index branch and linked worktree."""

    first_run: bool


@dataclass(frozen=True)
class IndexPackageCatalogTree:
    """One exact index revision and its generated package paths."""

    revision: str
    paths: tuple[PackageCatalogPath, ...]


class GitIndexRepository(GitCommandRunner):
    """Manage one sparse index worktree and its tracked tree."""

    def detach(self) -> None:
        """Detach the current worktree from its local branch."""

        self._run(("switch", "--detach"), required=True)

    def reset_hard(self, revision: str) -> None:
        """Reset the current worktree to a known fetched revision."""

        self._run(("reset", "--hard", revision), required=True)

    def set_sparse_root(self) -> None:
        """Materialize root files and persistent generated namespaces."""

        if not self.is_worktree():
            return
        self._run(("sparse-checkout", "init", "--cone"), required=True)
        self._run(("sparse-checkout", "set"), required=True)
        self._add_sparse_batch(_PERSISTENT_SPARSE_PATHS)

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
        selected = tuple(
            dict.fromkeys(
                (*_PERSISTENT_SPARSE_PATHS, *(path for path in paths if path))
            )
        )
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


def read_index_package_catalog(
    path: Path,
    known_revision: str | None = None,
) -> IndexPackageCatalogTree:
    """Read one index tree's package paths without hydrating package blobs."""

    reader = _IndexPackageCatalogReader(path)
    revision = reader.revision()
    if revision == known_revision:
        return IndexPackageCatalogTree(revision, ())
    return IndexPackageCatalogTree(revision, reader.package_paths())


class _IndexPackageCatalogReader(GitCommandRunner):
    """Read catalog paths through one credential-safe Git command runner."""

    def revision(self) -> str:
        """Return the current revision after verifying the index worktree."""

        worktree = self._run(("rev-parse", "--is-inside-work-tree"))
        if worktree.returncode != 0 or worktree.stdout.strip() != "true":
            raise WorkspaceError(f"index path is not a Git worktree: {self.path}")
        return self._run(("rev-parse", "HEAD"), required=True).stdout.strip()

    def package_paths(self) -> tuple[PackageCatalogPath, ...]:
        """Return package paths from tracked tree names only."""

        result = self._run(
            (
                "-c",
                "gc.auto=0",
                "ls-tree",
                "-r",
                "-z",
                "--name-only",
                "HEAD",
            ),
            required=True,
        )
        packages: list[PackageCatalogPath] = []
        for value in result.stdout.split("\0"):
            parts = PurePosixPath(value).parts
            if len(parts) != _PACKAGE_PATH_PARTS:
                continue
            filename = parts[_PACKAGE_FILENAME_INDEX]
            if filename == ".json" or not filename.endswith(".json"):
                continue
            package = filename.removesuffix(".json")
            if package:
                packages.append(PackageCatalogPath(parts[0], parts[1], package))
        return tuple(packages)


class IndexWorkspacePreparer:  # pylint: disable=too-few-public-methods
    """Prepare the remote index branch and its sparse linked worktree."""

    def __init__(
        self,
        repository: GitSourceRepository,
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
        index_repository = GitIndexRepository(
            index_dir,
            environment=self.repository.command_environment,
            redacted_values=self.repository.redacted_values,
        )
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
            GitIndexRepository(
                backup,
                environment=self.repository.command_environment,
                redacted_values=self.repository.redacted_values,
            ).detach()
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
