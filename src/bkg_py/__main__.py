"""Command-line entry point for bkg's incrementally migrated Python logic."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, NoReturn

from .config import RuntimeConfig
from .database import (
    DatabaseError,
    DatabaseRepository,
    DatabaseSettings,
    PackageRef,
    VersionStage,
)
from .owner_queue import OwnerQueueSelector
from .publication import PublicationError, publish_json_file, write_xml_file
from .rendering import (
    AggregateSettings,
    DatabaseAggregateOptions,
    PackageRenderOptions,
    RenderingError,
    render_database_aggregate,
    render_file_aggregate,
    render_package_file,
    render_version_array,
)
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
    database_parser = subparsers.add_parser(
        "database",
        help="run a migrated SQLite repository operation",
    )
    database_commands = database_parser.add_subparsers(
        dest="database_command",
        required=True,
    )
    database_commands.add_parser(
        "ensure-schema",
        help="lazily create normalized tables and indexes",
    )
    flush_parser = database_commands.add_parser(
        "flush-version-stage",
        help="transactionally commit a staged version batch",
    )
    flush_parser.add_argument("directory")
    cleanup_parser = database_commands.add_parser(
        "cleanup-legacy-package",
        help="drop one verified legacy version table",
    )
    cleanup_parser.add_argument("owner_id")
    cleanup_parser.add_argument("owner_type")
    cleanup_parser.add_argument("package_type")
    cleanup_parser.add_argument("owner")
    cleanup_parser.add_argument("repo")
    cleanup_parser.add_argument("package")
    cleanup_parser.add_argument("legacy_table")
    cleanup_parser.add_argument("since")
    cleanup_all_parser = database_commands.add_parser(
        "cleanup-legacy-all",
        help="clean verified and orphaned legacy tables during rotation",
    )
    cleanup_all_parser.add_argument("since")
    _add_render_parsers(subparsers)
    return parser


def _add_render_parsers(
    subparsers: Any,
) -> None:
    render_parser = subparsers.add_parser(
        "render",
        help="render migrated package and aggregate JSON",
    )
    render_commands = render_parser.add_subparsers(
        dest="render_command",
        required=True,
    )
    version_parser = render_commands.add_parser(
        "versions",
        help="print one package version array from SQLite",
    )
    _add_package_arguments(version_parser)
    version_parser.add_argument("legacy_table")
    version_parser.add_argument("since")
    version_parser.add_argument("version_limit", type=int)
    package_parser = render_commands.add_parser(
        "package",
        help="render one package JSON file from SQLite",
    )
    _add_package_arguments(package_parser)
    package_parser.add_argument("legacy_table")
    package_parser.add_argument("since")
    package_parser.add_argument("output_date")
    package_parser.add_argument("version_limit", type=int)
    package_parser.add_argument("destination")
    files_parser = render_commands.add_parser(
        "aggregate-files",
        help="render an aggregate from package JSON files",
    )
    files_parser.add_argument("source_directory")
    files_parser.add_argument("destination")
    database_render_parser = render_commands.add_parser(
        "aggregate-database",
        help="render an owner or repository aggregate from SQLite",
    )
    database_render_parser.add_argument("owner_id")
    database_render_parser.add_argument("repo")
    database_render_parser.add_argument("size_hint_directory")
    database_render_parser.add_argument("destination")
    repository_parser = render_commands.add_parser(
        "repositories",
        help="print repository names for one owner",
    )
    repository_parser.add_argument("owner_id")


def _add_package_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("owner_id")
    parser.add_argument("owner_type")
    parser.add_argument("package_type")
    parser.add_argument("owner")
    parser.add_argument("repo")
    parser.add_argument("package")


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


def _package_ref(args: argparse.Namespace) -> PackageRef:
    return PackageRef(
        owner_id=args.owner_id,
        owner_type=args.owner_type,
        package_type=args.package_type,
        owner=args.owner,
        repo=args.repo,
        package=args.package,
    )


def _run_database(args: argparse.Namespace) -> ExitStatus:
    try:
        stop = _stop_controller()
        repository = DatabaseRepository(
            DatabaseSettings.from_env(),
            check_stop=stop.check,
            sleep=stop.sleep,
        )
        with stop.signal_handlers():
            if args.database_command == "ensure-schema":
                repository.ensure_schema()
            elif args.database_command == "flush-version-stage":
                repository.flush_version_stage(VersionStage.load(Path(args.directory)))
            elif args.database_command == "cleanup-legacy-package":
                repository.cleanup_legacy_package(
                    _package_ref(args),
                    args.legacy_table,
                    since=args.since,
                )
            elif args.database_command == "cleanup-legacy-all":
                repository.cleanup_replaced_legacy_tables(since=args.since)
            else:
                raise DatabaseError(
                    f"unknown database command: {args.database_command}"
                )
    except GracefulStop:
        return ExitStatus.GRACEFUL_STOP
    except (DatabaseError, OSError) as error:
        print(error, file=sys.stderr)
        return ExitStatus.NON_FATAL
    return ExitStatus.SUCCESS


def _optional_argument(value: str) -> str | None:
    return None if value == "-" else value


def _run_render(args: argparse.Namespace) -> ExitStatus:
    try:
        stop = _stop_controller()
        with stop.signal_handlers():
            if args.render_command == "aggregate-files":
                render_file_aggregate(
                    Path(args.source_directory),
                    Path(args.destination),
                    settings=AggregateSettings.from_env(),
                    check_stop=stop.check,
                )
                return ExitStatus.SUCCESS

            repository = DatabaseRepository(
                DatabaseSettings.from_env(),
                check_stop=stop.check,
                sleep=stop.sleep,
            )
            if args.render_command == "versions":
                package = _package_ref(args)
                snapshot = repository.package_snapshot(
                    package,
                    since=args.since,
                    legacy_table=_optional_argument(args.legacy_table),
                )
                if snapshot is None:
                    raise RenderingError(
                        f"no package row found for {package.owner}/{package.package}"
                    )
                value = render_version_array(
                    snapshot.versions,
                    snapshot.package.record,
                    version_limit=args.version_limit,
                )
                print(
                    json.dumps(
                        value,
                        ensure_ascii=False,
                        allow_nan=False,
                        separators=(",", ":"),
                    )
                )
            elif args.render_command == "package":
                package = _package_ref(args)
                has_versions = render_package_file(
                    repository,
                    package,
                    Path(args.destination),
                    PackageRenderOptions(
                        since=args.since,
                        output_date=_optional_argument(args.output_date),
                        version_limit=args.version_limit,
                        legacy_table=_optional_argument(args.legacy_table),
                    ),
                    stop.check,
                )
                if not has_versions:
                    print(
                        f"No version rows available for "
                        f"{package.owner}/{package.package}; "
                        "using package-level fallback data",
                        file=sys.stderr,
                    )
            elif args.render_command == "aggregate-database":
                render_database_aggregate(
                    repository,
                    args.owner_id,
                    Path(args.destination),
                    DatabaseAggregateOptions(
                        repo=_optional_argument(args.repo),
                        size_hint_directory=(
                            None
                            if args.size_hint_directory == "-"
                            else Path(args.size_hint_directory)
                        ),
                        settings=AggregateSettings.from_env(),
                    ),
                    stop.check,
                )
            elif args.render_command == "repositories":
                sys.stdout.writelines(
                    f"{repo}\n" for repo in repository.repository_names(args.owner_id)
                )
            else:
                raise RenderingError(f"unknown render command: {args.render_command}")
    except GracefulStop:
        return ExitStatus.GRACEFUL_STOP
    except (DatabaseError, OSError, RenderingError) as error:
        print(error, file=sys.stderr)
        return ExitStatus.NON_FATAL
    return ExitStatus.SUCCESS


def main(argv: list[str] | None = None) -> ExitStatus:
    """Run a bkg Python subcommand and return its process exit status."""

    parser = build_parser()
    args = parser.parse_args(argv)
    status = ExitStatus.FAILURE

    if args.command == "config":
        print(json.dumps(RuntimeConfig.from_env().as_dict(), sort_keys=True))
        status = ExitStatus.SUCCESS
    elif args.command == "validate":
        status = validate_generated_file(args.file)
    elif args.command == "select-owners":
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
        status = ExitStatus.SUCCESS
    elif args.command in {"publish", "json-to-xml"}:
        status = _run_publication(args.command, args.file)
    elif args.command == "database":
        status = _run_database(args)
    elif args.command == "render":
        status = _run_render(args)
    else:
        parser.error(f"unknown command: {args.command}")
    return status


def entrypoint() -> NoReturn:
    """Run the installed bkg command."""

    raise SystemExit(main())


if __name__ == "__main__":
    entrypoint()
