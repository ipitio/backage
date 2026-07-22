"""Command adapters for repository workspace operations."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ..result import ExitStatus
from .handoff import HandoffSettings, WorkflowHandoffControl
from .layout import WorkspaceLayout
from .payload import import_workflow_payload
from .publication import UpdateWorkspacePublisher, published_run_status
from .repository import (
    GitRepository,
    IndexWorkspacePreparer,
    WorkspaceError,
    ensure_pages_root,
)


def _write_progress(message: str) -> None:
    sys.stderr.write(f"{message}\n")


def _write_stdout(message: str) -> None:
    sys.stdout.write(f"{message}\n")


def run_handoff(args: argparse.Namespace) -> ExitStatus:
    """Run one control-ref command without constructing application services."""

    control = WorkflowHandoffControl(
        Path(args.repository),
        HandoffSettings.from_env(),
        progress=_write_stdout,
        diagnostic=_write_progress,
    )
    try:
        if args.handoff_command == "baseline":
            sys.stdout.write(f"{control.current_baseline()}\n")
        elif args.handoff_command == "request":
            control.request()
        else:
            raise WorkspaceError(f"unknown handoff command: {args.handoff_command}")
    except (OSError, WorkspaceError) as error:
        _write_progress(str(error))
        return ExitStatus.NON_FATAL
    return ExitStatus.SUCCESS


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
    if command in {"configure-repository", "prepare-index", "publish-update"}:
        return _run_repository_command(args)
    if command in {"sparse-root", "sparse-add", "sparse-replace"}:
        _run_sparse_command(command, Path(args.index_dir))
        return ExitStatus.SUCCESS
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
    elif command == "top-level-count":
        count = GitRepository(Path(args.index_dir)).top_level_directory_count()
        sys.stdout.write(f"{count}\n")
    elif command == "ensure-pages":
        ensure_pages_root(Path(args.index_dir))
    else:
        raise WorkspaceError(f"unknown workspace command: {command}")
    return ExitStatus.SUCCESS


def _run_sparse_command(command: str, index_dir: Path) -> None:
    repository = GitRepository(index_dir)
    if command == "sparse-root":
        repository.set_sparse_root()
        return
    paths = (line.rstrip("\n") for line in sys.stdin)
    repository.materialize_sparse_paths(
        paths,
        replace=command == "sparse-replace",
    )


def _run_repository_command(args: argparse.Namespace) -> ExitStatus:
    if args.workspace_command == "configure-repository":
        GitRepository(Path(args.root)).configure_for_updates(args.actor)
    elif args.workspace_command == "prepare-index":
        result = IndexWorkspacePreparer(
            GitRepository(Path(args.root)),
            progress=_write_progress,
        ).prepare(args.index_branch, Path(args.index_dir))
        sys.stdout.write(f"{str(result.first_run).lower()}\n")
    else:
        status = published_run_status(args.run_status)
        if status is not ExitStatus.SUCCESS:
            _write_progress(
                f"Skipping Git publication after run status {args.run_status}"
            )
            return status
        UpdateWorkspacePublisher(
            Path(args.root),
            progress=_write_progress,
        ).publish(
            args.index_branch,
            Path(args.index_dir),
            Path(args.state_file),
        )
        return status
    return ExitStatus.SUCCESS
