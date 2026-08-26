"""Clone-local merge behavior for deployment-owned source inputs."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..files import atomic_text_output
from .git import GitCommandRunner, WorkspaceError
from .source import GitSourceRepository

_MERGE_DRIVER = "bkg-local"
_ATTRIBUTE_COMMENT = "# Preserve deployment-local bkg inputs during merges."
FORK_LOCAL_PATHS = ("owners.txt", "optout.txt", "README.md")
_MERGE_ATTRIBUTES = tuple(f"{path} merge={_MERGE_DRIVER}" for path in FORK_LOCAL_PATHS)


class GitLocalConfiguration(GitCommandRunner):
    """Read and write clone-local Git administration settings."""

    def git_path(self, path: str) -> Path:
        """Resolve one repository administration path through Git."""

        if not path:
            raise WorkspaceError("Git administration path is required")
        value = self._run(
            ("rev-parse", "--git-path", path),
            required=True,
        ).stdout.strip()
        if not value:
            raise WorkspaceError(f"Git returned an empty path for {path}")
        resolved = Path(value)
        if not resolved.is_absolute():
            resolved = self.path / resolved
        return resolved.resolve()

    def set_value(self, key: str, value: str) -> None:
        """Set one clone-local Git configuration value."""

        if not key:
            raise WorkspaceError("Git configuration key is required")
        self._run(("config", "--local", key, value), required=True)


@dataclass(frozen=True)
class ForkMergeConfiguration:
    """Location and change status for one configured clone."""

    attributes_path: Path
    attributes_changed: bool


def configure_fork_merge(repository_path: Path) -> ForkMergeConfiguration:
    """Keep deployment-owned inputs when upstream is merged into this clone."""

    resolved_path = repository_path.resolve()
    repository = GitSourceRepository(resolved_path)
    if not repository.is_worktree():
        raise WorkspaceError(f"not a Git worktree: {resolved_path}")
    local = GitLocalConfiguration(resolved_path)
    local.set_value(
        f"merge.{_MERGE_DRIVER}.name",
        "Keep deployment-local bkg inputs",
    )
    local.set_value(f"merge.{_MERGE_DRIVER}.driver", "true")
    attributes_path = local.git_path("info/attributes")
    changed = _ensure_attributes(attributes_path)
    return ForkMergeConfiguration(attributes_path, changed)


def _ensure_attributes(path: Path) -> bool:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []
    missing = tuple(entry for entry in _MERGE_ATTRIBUTES if entry not in lines)
    if not missing:
        return False

    updated = list(lines)
    if updated and updated[-1]:
        updated.append("")
    if _ATTRIBUTE_COMMENT not in updated:
        updated.append(_ATTRIBUTE_COMMENT)
    updated.extend(missing)
    path.parent.mkdir(parents=True, exist_ok=True)
    with atomic_text_output(path) as output:
        output.write("\n".join(updated))
        output.write("\n")
    return True
