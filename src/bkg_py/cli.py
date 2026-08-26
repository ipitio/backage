"""Parse and dispatch the supported bkg command-line interface."""

import argparse
import sys
from datetime import date
from typing import Any, NoReturn

from .commands import run_command
from .result import PUBLIC_EXIT_STATUSES, ExitStatus


def _iso_date(value: str) -> date:
    parsed = date.fromisoformat(value)
    if value != parsed.isoformat():
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    """Create the bkg command-line parser."""

    parser = argparse.ArgumentParser(prog="bkg")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser(
        "config",
        help="print the runtime configuration as JSON",
    )
    subparsers.add_parser(
        "reference",
        help="print the supported runtime surface as JSON",
    )
    release_parser = subparsers.add_parser(
        "release-tag",
        help="print the fortnightly release tag for a UTC date",
    )
    release_parser.add_argument("-D", "--run-date", type=_iso_date, required=True)
    vacuum_parser = subparsers.add_parser(
        "vacuum-releases",
        help="retain restore points and database rotation archives",
    )
    vacuum_parser.add_argument("-o", "--owner")
    vacuum_parser.add_argument("-r", "--repository")
    vacuum_parser.add_argument("-n", "--dry-run", action="store_true")
    merge_parser = subparsers.add_parser(
        "configure-fork-merge",
        help="preserve deployment-owned files during upstream merges",
    )
    merge_parser.add_argument("repository", nargs="?", default=".")
    sync_parser = subparsers.add_parser(
        "workflow-sync-fork",
        help="project material upstream source changes into a deployment fork",
    )
    sync_parser.add_argument("-C", "--repository", default=".")
    sync_parser.add_argument("-u", "--upstream", required=True)
    sync_parser.add_argument("-b", "--upstream-branch", required=True)
    _add_run_parser(subparsers)
    validate_parser = subparsers.add_parser(
        "validate",
        help="validate a generated JSON or XML file",
    )
    validate_parser.add_argument("file")
    _add_handoff_parser(subparsers)
    _add_workflow_update_parser(subparsers)
    return parser


def _add_run_parser(subparsers: Any) -> None:
    run_parser = subparsers.add_parser(
        "run",
        help="run the application lifecycle in an existing workspace",
    )
    run_parser.add_argument("-d", "--duration", type=int)
    run_parser.add_argument("-m", "--mode", type=int, choices=range(6))
    run_parser.add_argument(
        "-s",
        "--source-published-today",
        choices=("true", "false"),
        default="false",
    )
    run_parser.add_argument("-C", "--working-directory", default=".")
    run_parser.add_argument("-n", "--owner-request-limit", type=int, default=100)
    run_parser.add_argument("-D", "--run-date", type=_iso_date)


def _add_handoff_parser(subparsers: Any) -> None:
    handoff_parser = subparsers.add_parser(
        "handoff",
        help="read or advance the graceful workflow handoff ref",
    )
    handoff_commands = handoff_parser.add_subparsers(
        dest="handoff_command",
        required=True,
    )
    for command in ("baseline", "request"):
        command_parser = handoff_commands.add_parser(command)
        command_parser.add_argument("repository", nargs="?", default=".")
    should_run_parser = handoff_commands.add_parser(
        "should-run",
        help="decide whether a serialized scheduled update is still current",
    )
    should_run_parser.add_argument("queued_baseline")
    should_run_parser.add_argument("current_baseline")
    should_run_parser.add_argument("run_id")
    should_run_parser.add_argument("latest_scheduled_run_id", nargs="?", default="")
    should_run_parser.add_argument("active_manual_run_id", nargs="?", default="")
    handoff_commands.add_parser(
        "workflow-runs",
        help="select freshness run IDs from an Actions API response on stdin",
    )


def _add_workflow_update_parser(subparsers: Any) -> None:
    update_parser = subparsers.add_parser(
        "workflow-update",
        help="run the repository update and publication lifecycle",
    )
    update_parser.add_argument(
        "root",
        nargs="?",
        metavar="ROOT",
        help="repository root; retained as a positional form for compatibility",
    )
    update_parser.add_argument(
        "-r",
        "--root",
        dest="named_root",
        metavar="ROOT",
        help="repository root (default: bkg)",
    )
    update_parser.add_argument("-d", "--duration", type=int)
    update_parser.add_argument("-m", "--mode", type=int, choices=range(6))
    update_parser.add_argument(
        "-n",
        "--owner-request-limit",
        type=int,
        default=100,
    )
    update_parser.add_argument("-C", "--invocation-directory", default=".")
    update_parser.add_argument("-p", "--payload-directory")
    update_parser.add_argument("-D", "--run-date", type=_iso_date)


def main(argv: list[str] | None = None) -> ExitStatus:
    """Run a bkg Python subcommand and return its process exit status."""

    parser = build_parser()
    return run_command(parser.parse_args(argv), parser)


def entrypoint() -> NoReturn:
    """Run the installed bkg command."""

    status = main()
    if status not in PUBLIC_EXIT_STATUSES:
        sys.stderr.write(
            f"Unexpected bkg status {int(status)} ({status.name}); "
            f"returning {int(ExitStatus.NON_FATAL)}\n"
        )
        status = ExitStatus.NON_FATAL
    raise SystemExit(status)
