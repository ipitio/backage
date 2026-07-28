"""Command adapter for workflow handoff operations."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ..result import ExitStatus
from .handoff import (
    HandoffSettings,
    WorkflowHandoffControl,
    scheduled_update_skip_reason,
    workflow_run_freshness,
)
from .repository import WorkspaceError


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
