"""Clone-local merge behavior for deployment-owned source inputs."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..files import atomic_text_output
from .repository import GitLocalConfiguration, GitRepository, WorkspaceError

_MERGE_DRIVER = "bkg-local"
_ATTRIBUTE_COMMENT = "# Preserve deployment-local bkg inputs during merges."
_MERGE_ATTRIBUTES = (
    f"owners.txt merge={_MERGE_DRIVER}",
    f"optout.txt merge={_MERGE_DRIVER}",
    f"README.md merge={_MERGE_DRIVER}",
)


@dataclass(frozen=True)
class ForkMergeConfiguration:
    """Location and change status for one configured clone."""

    attributes_path: Path
    attributes_changed: bool


def configure_fork_merge(repository_path: Path) -> ForkMergeConfiguration:
    """Keep deployment-owned inputs when upstream is merged into this clone."""

    resolved_path = repository_path.resolve()
    repository = GitRepository(resolved_path)
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
