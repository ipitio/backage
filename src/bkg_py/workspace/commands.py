"""Command adapters for repository workspace operations."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ..config import SettingsSnapshot
from ..result import ExitStatus
from ..runtime_names import EnvironmentVariable as Env
from .fork_sync import synchronize_fork_source
from .git import WorkspaceError
from .handoff import (
    GitControlRefRepository,
    WorkflowHandoffControl,
    scheduled_update_skip_reason,
    workflow_run_freshness,
)
from .merge_configuration import configure_fork_merge
from .settings import HandoffSettings


def _write_progress(message: str) -> None:
    sys.stderr.write(f"{message}\n")


def _write_stdout(message: str) -> None:
    sys.stdout.write(f"{message}\n")


def run_handoff(args: argparse.Namespace) -> ExitStatus:
    """Run one control-ref command without constructing application services."""

    if args.handoff_command == "should-run":
        reason = scheduled_update_skip_reason(
            args.queued_baseline,
            args.current_baseline,
            args.run_id,
            args.latest_scheduled_run_id,
            args.active_manual_run_id,
        )
        if reason is not None:
            _write_stdout(reason)
            return ExitStatus.NON_FATAL
        return ExitStatus.SUCCESS
    if args.handoff_command == "workflow-runs":
        try:
            latest_scheduled, active_manual = workflow_run_freshness(
                json.load(sys.stdin)
            )
        except (json.JSONDecodeError, ValueError) as error:
            _write_progress(str(error))
            return ExitStatus.NON_FATAL
        _write_stdout(f"{latest_scheduled}|{active_manual}")
        return ExitStatus.SUCCESS

    values = SettingsSnapshot.from_env()
    redacted_values = tuple(
        value
        for name in (Env.GITHUB_TOKEN, Env.GH_TOKEN)
        if (value := values.get(name))
    )
    control = WorkflowHandoffControl(
        GitControlRefRepository(
            Path(args.repository),
            environment=values,
            redacted_values=redacted_values,
        ),
        HandoffSettings.from_mapping(values),
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


def run_fork_merge_configuration(args: argparse.Namespace) -> ExitStatus:
    """Configure deployment-owned files for ordinary upstream merges."""

    try:
        result = configure_fork_merge(Path(args.repository))
    except (OSError, WorkspaceError) as error:
        _write_progress(str(error))
        return ExitStatus.NON_FATAL
    action = "Updated" if result.attributes_changed else "Verified"
    _write_stdout(
        f"{action} fork-local merge handling in {result.attributes_path.parent}"
    )
    return ExitStatus.SUCCESS


def run_fork_sync(args: argparse.Namespace) -> ExitStatus:
    """Synchronize one workflow checkout with material upstream changes."""

    try:
        result = synchronize_fork_source(
            Path(args.repository),
            upstream_url=args.upstream,
            upstream_branch=args.upstream_branch,
        )
    except (OSError, WorkspaceError) as error:
        _write_progress(str(error))
        return ExitStatus.NON_FATAL
    _write_stdout(f"updated={'true' if result.updated else 'false'}")
    _write_stdout(f"upstream_sha={result.upstream_commit}")
    return ExitStatus.SUCCESS
