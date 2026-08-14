"""Execute the supported bkg command adapters."""
# pylint: disable=import-outside-toplevel

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import ConfigError, RepositoryMaintenanceSettings, SettingsSnapshot
from .result import ExitStatus


def run_command(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
) -> ExitStatus:
    """Execute one parsed command and return its process status."""

    try:
        return _dispatch_command(args, parser)
    except ConfigError as error:
        print(str(error), file=sys.stderr)
        return ExitStatus.NON_FATAL


def _dispatch_command(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
) -> ExitStatus:
    """Dispatch one parsed command after process-boundary error handling."""

    if args.command in {"config", "reference", "release-tag"}:
        return _run_information_command(args, parser)
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
    if args.command in {"configure-fork-merge", "vacuum-releases"}:
        return _run_repository_maintenance(args)
    if args.command == "workflow-update":
        return _run_workflow_update(args, parser)
    parser.error(f"unknown command: {args.command}")
    raise AssertionError("argparse.error must exit")


def _run_information_command(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
) -> ExitStatus:
    if args.command == "config":
        from .application import ApplicationSettings
        from .workspace.settings import WorkspaceSettings

        values = SettingsSnapshot.from_env()
        output = ApplicationSettings.from_mapping(values).as_dict()
        output["workspace"] = WorkspaceSettings.from_mapping(values).as_dict()
        print(json.dumps(output, sort_keys=True))
    elif args.command == "reference":
        from .reference import build_runtime_reference

        print(json.dumps(build_runtime_reference(parser), sort_keys=True))
    else:
        from .release import release_tag

        print(release_tag(args.run_date))
    return ExitStatus.SUCCESS


def _run_repository_maintenance(args: argparse.Namespace) -> ExitStatus:
    if args.command == "configure-fork-merge":
        from .workspace.commands import run_fork_merge_configuration

        return run_fork_merge_configuration(args)

    from .github import GitHubClient, GitHubError, GitHubSettings
    from .release_retention import ReleaseRetentionError, apply_release_retention

    settings = SettingsSnapshot.from_env()
    repository = RepositoryMaintenanceSettings.from_mapping(settings)
    owner = args.owner or repository.owner or ""
    repo = args.repository or repository.name or ""
    try:
        with GitHubClient(GitHubSettings.from_mapping(settings)) as client:
            apply_release_retention(
                client,
                owner=owner,
                repo=repo,
                dry_run=args.dry_run,
                progress=print,
            )
    except (GitHubError, OSError, ReleaseRetentionError) as error:
        print(str(error), file=sys.stderr)
        return ExitStatus.NON_FATAL
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
