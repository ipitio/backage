"""Parse and dispatch the bkg command-line interface."""

from __future__ import annotations

import argparse
import sys
from typing import Any, NoReturn

from .commands import run_command
from .result import PUBLIC_EXIT_STATUSES, ExitStatus


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
    select_parser.add_argument(
        "--reasons-file",
        help="write selected owner names and queue reasons as tab-separated rows",
    )
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
    _add_database_parsers(subparsers)
    _add_render_parsers(subparsers)
    _add_snapshot_parsers(subparsers)
    _add_github_parsers(subparsers)
    return parser


def _add_database_parsers(subparsers: Any) -> None:
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
    retire_owner_parser = database_commands.add_parser(
        "retire-owner",
        help="remove database data for one unavailable owner",
    )
    retire_owner_parser.add_argument("owner")
    _add_owner_scan_database_parsers(database_commands)


def _add_owner_scan_database_parsers(database_commands: Any) -> None:
    """Add owner scan database subcommands."""

    begin_scan_parser = database_commands.add_parser(
        "begin-owner-scan",
        help="start a fresh resumable owner package-listing scan",
    )
    begin_scan_parser.add_argument("owner_id")
    begin_scan_parser.add_argument("owner")
    begin_scan_parser.add_argument("marker")
    begin_scan_parser.add_argument("started_at", type=int)
    active_scan_parser = database_commands.add_parser(
        "active-owner-scan",
        help="check whether an owner scan marker is still resumable",
    )
    active_scan_parser.add_argument("owner_id")
    active_scan_parser.add_argument("marker")
    observe_scan_parser = database_commands.add_parser(
        "observe-owner-scan",
        help="stage package identities parsed from an owner listing page",
    )
    observe_scan_parser.add_argument("owner_id")
    observe_scan_parser.add_argument("marker")
    observe_scan_parser.add_argument("packages_file")
    observe_scan_parser.add_argument("observed_at", type=int)
    missing_scan_parser = database_commands.add_parser(
        "missing-owner-scan-packages",
        help="print known packages absent from the staged owner listing",
    )
    missing_scan_parser.add_argument("owner_id")
    missing_scan_parser.add_argument("marker")
    complete_scan_parser = database_commands.add_parser(
        "complete-owner-scan",
        help="transactionally reconcile one verified owner listing scan",
    )
    complete_scan_parser.add_argument("owner_id")
    complete_scan_parser.add_argument("marker")
    complete_scan_parser.add_argument("scan_date")
    complete_scan_parser.add_argument("completed_at", type=int)
    fail_scan_parser = database_commands.add_parser(
        "fail-owner-scan",
        help="record owner retry backoff after failed scan or refresh work",
    )
    fail_scan_parser.add_argument("owner_id")
    fail_scan_parser.add_argument("owner")
    fail_scan_parser.add_argument("marker")
    fail_scan_parser.add_argument("error")
    fail_scan_parser.add_argument("failed_at", type=int)
    clear_backoff_parser = database_commands.add_parser(
        "clear-owner-backoff",
        help="clear owner retry state after successful direct refresh work",
    )
    clear_backoff_parser.add_argument("owner_id")
    clear_backoff_parser.add_argument("owner")
    clear_backoff_parser.add_argument("completed_at", type=int)
    deferred_parser = database_commands.add_parser(
        "deferred-owners",
        help="print owners whose retry time has not arrived",
    )
    deferred_parser.add_argument("now", type=int)


def _add_render_parsers(subparsers: Any) -> None:
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


def _add_snapshot_parsers(subparsers: Any) -> None:
    snapshot_parser = subparsers.add_parser(
        "snapshot",
        help="run migrated local database snapshot operations",
    )
    snapshot_commands = snapshot_parser.add_subparsers(
        dest="snapshot_command",
        required=True,
    )
    snapshot_commands.add_parser(
        "current-archive",
        help="print the selected local snapshot archive path",
    )
    snapshot_commands.add_parser(
        "current-signature",
        help="print the selected local snapshot archive SHA-256",
    )
    path_parser = snapshot_commands.add_parser(
        "path",
        help="print a configured local snapshot path",
    )
    path_parser.add_argument(
        "kind",
        choices=("db", "db-zst", "sql-zst", "restore-signature"),
    )
    asset_parser = snapshot_commands.add_parser(
        "asset-name",
        help="print a configured snapshot release asset name",
    )
    asset_parser.add_argument("kind", choices=("db", "db-zst", "sql-zst"))
    snapshot_commands.add_parser(
        "restore-signature-matches",
        help="exit successfully when the database matches the selected archive",
    )
    snapshot_commands.add_parser(
        "restore-if-needed",
        help="restore the local database from the selected archive when needed",
    )
    restore_archive_parser = snapshot_commands.add_parser(
        "restore-archive-if-needed",
        help="restore the local database from a configured archive path when needed",
    )
    restore_archive_parser.add_argument("archive")
    snapshot_commands.add_parser(
        "write-restore-signature",
        help="write the selected archive SHA-256 restore signature",
    )
    snapshot_commands.add_parser(
        "checkpoint",
        help="checkpoint the SQLite database before snapshot archiving",
    )
    snapshot_commands.add_parser(
        "prepare",
        help="checkpoint and atomically prepare the current database archive",
    )
    download_parser = snapshot_commands.add_parser(
        "download-release",
        help="download and restore a snapshot asset from a GitHub release",
    )
    download_parser.add_argument("tag", nargs="?")
    download_parser.add_argument(
        "--check",
        action="store_true",
        help="only verify that the release has a supported snapshot asset",
    )
    rotate_parser = snapshot_commands.add_parser(
        "rotate-if-needed",
        help="archive and prune an oversized database before snapshot publication",
    )
    rotate_parser.add_argument("threshold_bytes", type=int)
    rotate_parser.add_argument("since")
    rotate_parser.add_argument("date_stamp")


def _add_github_parsers(subparsers: Any) -> None:
    github_parser = subparsers.add_parser(
        "github",
        help="run a pooled GitHub HTTP operation",
    )
    github_commands = github_parser.add_subparsers(
        dest="github_command",
        required=True,
    )
    rest_parser = github_commands.add_parser(
        "rest",
        help="request one REST API path as JSON",
    )
    rest_parser.add_argument("path")
    rest_parser.add_argument(
        "--missing-ok",
        action="store_true",
        help="print null instead of failing when GitHub returns HTTP 404",
    )
    github_commands.add_parser(
        "graphql",
        help="execute a GraphQL query read from standard input",
    )
    download_parser = github_commands.add_parser(
        "download",
        help="stream a URL into an atomic destination",
    )
    download_parser.add_argument("url")
    download_parser.add_argument("destination")
    download_parser.add_argument("--authenticated", action="store_true")


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
