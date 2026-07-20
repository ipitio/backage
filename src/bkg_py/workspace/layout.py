"""Derive source and index workspace paths from repository branches."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .repository import GitRepository, WorkspaceError


@dataclass(frozen=True)
class WorkspaceLayout:
    """Branch names and paths shared by update workspace operations."""

    root: Path
    source_branch: str
    github_branch: str
    index_name: str
    index_db: Path
    index_sql: Path
    index_dir: Path

    @classmethod
    def discover(
        cls,
        root: Path,
        requested_branch: str | None = None,
    ) -> WorkspaceLayout:
        """Inspect the repository and derive its complete workspace layout."""

        resolved_root = root.resolve()
        source_branch = GitRepository(resolved_root).current_branch()
        return cls.from_branches(
            resolved_root,
            source_branch=source_branch,
            requested_branch=requested_branch,
        )

    @classmethod
    def from_branches(
        cls,
        root: Path,
        *,
        source_branch: str,
        requested_branch: str | None = None,
    ) -> WorkspaceLayout:
        """Derive a layout from branch values without inspecting Git."""

        resolved_root = root.resolve()
        github_branch = requested_branch or source_branch
        if not github_branch:
            raise WorkspaceError(
                "cannot determine the source branch from a detached checkout; "
                "set GITHUB_BRANCH"
            )
        index_name = "index" if github_branch == "master" else f"index-{github_branch}"
        return cls(
            root=resolved_root,
            source_branch=source_branch,
            github_branch=github_branch,
            index_name=index_name,
            index_db=resolved_root / f"{index_name}.db",
            index_sql=resolved_root / f"{index_name}.sql",
            index_dir=resolved_root / index_name,
        )

    def shell_fields(self) -> tuple[str, ...]:
        """Return the ordered fields consumed by the compatibility launcher."""

        return (
            self.source_branch,
            self.github_branch,
            self.index_name,
            str(self.index_db),
            str(self.index_sql),
            str(self.index_dir),
        )
