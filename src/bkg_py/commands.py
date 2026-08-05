"""Execute the supported bkg command adapters."""
# pylint: disable=import-outside-toplevel

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .result import ExitStatus


def run_command(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
) -> ExitStatus:
    """Execute one parsed command and return its process status."""

    if args.command in {"config", "release-tag"}:
        return _run_information_command(args)
    if args.command == "validate":
        from .validation import validate_generated_file

        return validate_generated_file(args.file)
    if args.command == "run":
        from .application import ApplicationContext
        from .run.commands import run_application

        return run_application(args, ApplicationContext.from_env())
    if args.command == "handoff":
        from .workspace.commands import run_handoff

        return run_handoff(args)
    if args.command == "configure-fork-merge":
        from .workspace.commands import run_fork_merge_configuration

        return run_fork_merge_configuration(args)
    if args.command == "workflow-update":
        return _run_workflow_update(args, parser)
    parser.error(f"unknown command: {args.command}")
    raise AssertionError("argparse.error must exit")


def _run_information_command(args: argparse.Namespace) -> ExitStatus:
    if args.command == "config":
        from .config import RuntimeConfig

        print(json.dumps(RuntimeConfig.from_env().as_dict(), sort_keys=True))
    else:
        from .release import release_tag

        print(release_tag(args.run_date))
    return ExitStatus.SUCCESS


def _run_workflow_update(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
) -> ExitStatus:
    from .workspace.update import (
        UpdateWorkflowExecution,
        UpdateWorkflowRequest,
        run_update_workflow,
    )

    return run_update_workflow(
        UpdateWorkflowRequest(
            root=_workflow_update_root(args, parser),
            invocation_directory=Path(args.invocation_directory),
            payload_directory=(
                None if args.payload_directory is None else Path(args.payload_directory)
            ),
            duration=args.duration,
            mode=args.mode,
            owner_request_limit=args.owner_request_limit,
            run_date=args.run_date,
        ),
        UpdateWorkflowExecution(
            progress=print,
            diagnostic=lambda message: print(message, file=sys.stderr),
        ),
    )


def _workflow_update_root(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
) -> Path:
    """Resolve the named, positional, or default workflow repository root."""

    if args.root is not None and args.named_root is not None:
        parser.error("ROOT and --root cannot be used together")
    return Path(args.named_root or args.root or "bkg")
