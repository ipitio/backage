"""Command adapters for repository workspace operations."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ..result import ExitStatus
from .layout import WorkspaceLayout
from .payload import import_workflow_payload
from .repository import GitRepository, WorkspaceError, ensure_pages_root


def run_workspace(args: argparse.Namespace) -> ExitStatus:
    """Run one workspace command without constructing application services."""

    try:
        status = _run_workspace_command(args)
    except (OSError, WorkspaceError) as error:
        sys.stderr.write(f"{error}\n")
        return ExitStatus.NON_FATAL
    return status


def _run_workspace_command(args: argparse.Namespace) -> ExitStatus:
    command = args.workspace_command
    if command == "layout":
        layout = WorkspaceLayout.discover(
            Path(args.root),
            requested_branch=args.requested_branch,
        )
        sys.stdout.write("\0".join(layout.shell_fields()) + "\0")
    elif command == "import-payload":
        import_workflow_payload(
            Path(args.payload_dir),
            Path(args.destination),
        )
    elif command == "is-repo":
        if not GitRepository(Path(args.index_dir)).is_worktree():
            return ExitStatus.NON_FATAL
    elif command == "sparse-root":
        GitRepository(Path(args.index_dir)).set_sparse_root()
    elif command == "sparse-add":
        GitRepository(Path(args.index_dir)).add_sparse_paths(
            line.rstrip("\n") for line in sys.stdin
        )
    elif command == "top-level-count":
        count = GitRepository(Path(args.index_dir)).top_level_directory_count()
        sys.stdout.write(f"{count}\n")
    elif command == "ensure-pages":
        ensure_pages_root(Path(args.index_dir))
    else:
        raise WorkspaceError(f"unknown workspace command: {command}")
    return ExitStatus.SUCCESS
