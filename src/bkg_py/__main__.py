"""Command-line entry point for bkg's incrementally migrated Python logic."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import NoReturn

from .config import RuntimeConfig
from .owner_queue import OwnerQueueSelector
from .publication import PublicationError, publish_json_file, write_xml_file
from .result import ExitStatus
from .runtime import GracefulStop, StopController
from .state import StateStore
from .validation import validate_generated_file


def build_parser() -> argparse.ArgumentParser:
    """Create the command-line parser for the Python migration helpers."""

    parser = argparse.ArgumentParser(prog="python -m bkg_py")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser(
        "config",
        help="print the Python view of the bkg runtime configuration as JSON",
    )
    validate_parser = subparsers.add_parser(
        "validate",
        help="validate a generated JSON or XML file",
    )
    validate_parser.add_argument("file")
    select_parser = subparsers.add_parser(
        "select-owners",
        help="select the next bounded owner candidate queue",
    )
    select_parser.add_argument("rest_first")
    select_parser.add_argument("connections_file")
    select_parser.add_argument("request_limit", type=int)
    select_parser.add_argument("current_owner")
    select_parser.add_argument("manual_file")
    select_parser.add_argument("index_dir")
    publish_parser = subparsers.add_parser(
        "publish",
        help="trim and publish a JSON file with its XML representation",
    )
    publish_parser.add_argument("file")
    xml_parser = subparsers.add_parser(
        "json-to-xml",
        help="publish an XML representation of one JSON file",
    )
    xml_parser.add_argument("file")
    return parser


def _stop_controller() -> StopController:
    config = RuntimeConfig.from_env()
    return StopController(
        StateStore(Path(config.env_file)),
        max_duration=config.max_len,
    )


def _run_publication(command: str, filename: str) -> ExitStatus:
    try:
        stop = _stop_controller()
        with stop.signal_handlers():
            if command == "publish":
                publish_json_file(Path(filename), stop.check)
            else:
                print(write_xml_file(Path(filename), stop.check))
    except GracefulStop:
        return ExitStatus.GRACEFUL_STOP
    except (OSError, PublicationError) as error:
        print(error, file=sys.stderr)
        return ExitStatus.NON_FATAL
    return ExitStatus.SUCCESS


def main(argv: list[str] | None = None) -> ExitStatus:
    """Run a bkg Python subcommand and return its process exit status."""

    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "config":
        print(json.dumps(RuntimeConfig.from_env().as_dict(), sort_keys=True))
        return ExitStatus.SUCCESS

    if args.command == "validate":
        return validate_generated_file(args.file)

    if args.command == "select-owners":
        selector = OwnerQueueSelector(
            rest_first=args.rest_first,
            connections_file=Path(args.connections_file),
            request_limit=args.request_limit,
            current_owner=args.current_owner,
            manual_file=Path(args.manual_file),
            index_dir=Path(args.index_dir),
            state_dir=Path.cwd(),
        )
        sys.stdout.writelines(f"{owner}\n" for owner in selector.select())
        return ExitStatus.SUCCESS

    if args.command in {"publish", "json-to-xml"}:
        return _run_publication(args.command, args.file)

    parser.error(f"unknown command: {args.command}")
    return ExitStatus.FAILURE


def entrypoint() -> NoReturn:
    """Run the installed bkg command."""

    raise SystemExit(main())


if __name__ == "__main__":
    entrypoint()
