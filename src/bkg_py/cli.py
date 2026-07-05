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
    _add_discovery_parsers(subparsers)
    _add_version_parsers(subparsers)
    _add_package_parsers(subparsers)
    _add_owner_parsers(subparsers)
    _add_orchestration_parsers(subparsers)
    return parser


def _add_orchestration_parsers(subparsers: Any) -> None:
    orchestration_parser = subparsers.add_parser(
        "orchestration",
        help="run migrated application orchestration decisions",
    )
    orchestration_commands = orchestration_parser.add_subparsers(
        dest="orchestration_command",
        required=True,
    )
    _add_run_state_parsers(orchestration_commands)
    _add_orchestration_operation_parsers(orchestration_commands)


def _add_run_state_parsers(orchestration_commands: Any) -> None:
    """Add run-state and package-plan orchestration commands."""

    begin_run_parser = orchestration_commands.add_parser(
        "begin-run",
        help="atomically initialize persisted state for one application run",
    )
    begin_run_parser.add_argument("today")
    begin_run_parser.add_argument("started_at", type=int)
    prepare_run_parser = orchestration_commands.add_parser(
        "prepare-run",
        help="initialize run state and publish the current package-work plan",
    )
    prepare_run_parser.add_argument("today")
    prepare_run_parser.add_argument("started_at", type=int)
    prepare_run_parser.add_argument("working_directory")
    complete_batch_parser = orchestration_commands.add_parser(
        "complete-batch-if-exhausted",
        help="atomically start a new batch when no package work remains",
    )
    complete_batch_parser.add_argument("today")
    complete_batch_parser.add_argument("remaining", type=int)
    daily_gate_parser = orchestration_commands.add_parser(
        "daily-gate-should-skip",
        help="check whether a daily phase is complete for this run context",
    )
    daily_gate_parser.add_argument("key")
    daily_gate_parser.add_argument("today")
    daily_gate_parser.add_argument(
        "source_published_today",
        choices=("true", "false"),
    )
    complete_gate_parser = orchestration_commands.add_parser(
        "complete-daily-gate",
        help="mark a daily phase complete for this run context",
    )
    complete_gate_parser.add_argument("key")
    complete_gate_parser.add_argument("today")
    owner_phase_parser = orchestration_commands.add_parser(
        "owner-phase-decision",
        help="decide whether owner-phase status permits snapshot publication",
    )
    owner_phase_parser.add_argument("phase_status", type=int)
    owner_phase_parser.add_argument("run_status", type=int, nargs="?", default=0)
    update_owners_parser = orchestration_commands.add_parser(
        "update-owners",
        help="update the persisted owner queue in one shared Python process",
    )
    update_owners_parser.add_argument("since")
    update_owners_parser.add_argument("batch_marker")
    update_owners_parser.add_argument("fast_out", choices=("true", "false"))
    package_plan_parser = orchestration_commands.add_parser(
        "prepare-package-plan",
        help="write package-work compatibility files from one database snapshot",
    )
    package_plan_parser.add_argument("since")
    package_plan_parser.add_argument("directory")
    package_plan_parser.add_argument(
        "reset",
        choices=("true", "false"),
        nargs="?",
        default="false",
    )


def _add_orchestration_operation_parsers(orchestration_commands: Any) -> None:
    """Add discovery, queue, and publication orchestration commands."""

    owner_queue_parser = orchestration_commands.add_parser(
        "prepare-owner-queue",
        help="resolve discovered candidates and persist the next owner queue",
    )
    owner_queue_parser.add_argument("rest_first")
    owner_queue_parser.add_argument("connections_file")
    owner_queue_parser.add_argument("request_limit", type=int)
    owner_queue_parser.add_argument("include_manual", choices=("true", "false"))
    owner_queue_parser.add_argument("working_directory")
    owner_queue_parser.add_argument("now", type=int)
    targeted_owner_queue_parser = orchestration_commands.add_parser(
        "prepare-targeted-owner-queue",
        help="resolve and queue the configured owner and discovered memberships",
    )
    targeted_owner_queue_parser.add_argument("connections_file")
    orchestration_commands.add_parser(
        "prepare-optout-owner-queue",
        help="resolve and queue owners named by configured opt-out entries",
    )
    discovery_phase_parser = orchestration_commands.add_parser(
        "discover-owners",
        help="run the authenticated global or membership discovery phase",
    )
    discovery_phase_parser.add_argument("today")
    discovery_phase_parser.add_argument("skip_explore", choices=("true", "false"))
    discovery_phase_parser.add_argument("connections_file")
    discovery_phase_parser.add_argument("packages_all_file")
    run_publication_parser = orchestration_commands.add_parser(
        "publish-run-summary",
        help="publish final source and index summaries from committed database rows",
    )
    run_publication_parser.add_argument("today")
    run_publication_parser.add_argument("rotated", choices=("true", "false"))
    run_publication_parser.add_argument("working_directory")
    finalization_parser = orchestration_commands.add_parser(
        "finalize-run",
        help="prepare the database snapshot and publish final run summaries",
    )
    finalization_parser.add_argument("today")
    finalization_parser.add_argument("prepare_snapshot", choices=("true", "false"))
    finalization_parser.add_argument("working_directory")


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


def _add_version_parsers(subparsers: Any) -> None:
    version_parser = subparsers.add_parser(
        "version",
        help="run migrated package-version helpers",
    )
    version_commands = version_parser.add_subparsers(
        dest="version_command",
        required=True,
    )
    parse_page_parser = version_commands.add_parser(
        "parse-page-html",
        help="parse a GitHub package version listing page from stdin",
    )
    parse_page_parser.add_argument("owner_type")
    parse_page_parser.add_argument("owner")
    parse_page_parser.add_argument("repo")
    parse_page_parser.add_argument("package_type")
    parse_page_parser.add_argument("package")
    version_commands.add_parser(
        "extract-embedded-manifest",
        help="extract the manifest JSON block from a GitHub version page on stdin",
    )
    version_commands.add_parser(
        "extract-page-data",
        help="extract migrated version-page data from stdin as JSON",
    )
    manifest_size_parser = version_commands.add_parser(
        "manifest-size",
        help="calculate a container manifest size from stdin",
    )
    manifest_size_parser.add_argument("context", nargs="?")
    version_commands.add_parser(
        "cache-candidates",
        help="normalize version page JSON into shell cache records",
    )
    refresh_parser = version_commands.add_parser(
        "refresh-package",
        help="refresh one package's selected version rows",
    )
    _add_package_arguments(refresh_parser)
    refresh_parser.add_argument("legacy_table")
    refresh_parser.add_argument("since")
    refresh_parser.add_argument("write_legacy", choices=("true", "false"))
    refresh_parser.add_argument("use_rest_api", choices=("true", "false"))


def _add_package_parsers(subparsers: Any) -> None:
    package_parser = subparsers.add_parser(
        "package",
        help="run migrated package metadata operations",
    )
    package_commands = package_parser.add_subparsers(
        dest="package_command",
        required=True,
    )
    active_scan_parser = package_commands.add_parser(
        "active-scan",
        help="print the current batch's durable owner scan cursor",
    )
    active_scan_parser.add_argument("owner_id")
    active_scan_parser.add_argument("batch_marker")
    begin_scan_parser = package_commands.add_parser(
        "begin-scan",
        help="resume or start a durable owner package listing scan",
    )
    begin_scan_parser.add_argument("owner_id")
    begin_scan_parser.add_argument("owner")
    begin_scan_parser.add_argument("batch_marker")
    begin_scan_parser.add_argument("started_at", type=int)
    listing_parser = package_commands.add_parser(
        "list-page",
        help="fetch and stage one owner package listing page",
    )
    listing_parser.add_argument("owner_id")
    listing_parser.add_argument("owner_type", choices=("orgs", "users"))
    listing_parser.add_argument("owner")
    listing_parser.add_argument("page", type=int)
    listing_parser.add_argument("marker")
    listing_parser.add_argument("since")
    listing_parser.add_argument("observed_at", type=int)
    finish_page_parser = package_commands.add_parser(
        "finish-page",
        help="advance a durable owner scan after its package work finishes",
    )
    finish_page_parser.add_argument("owner_id")
    finish_page_parser.add_argument("marker")
    finish_page_parser.add_argument("page", type=int)
    finish_page_parser.add_argument("completed_at", type=int)
    observe_refs_parser = package_commands.add_parser(
        "observe-refs",
        help="stage verified package refs and print those needing refresh",
    )
    observe_refs_parser.add_argument("owner_id")
    observe_refs_parser.add_argument("owner")
    observe_refs_parser.add_argument("marker")
    observe_refs_parser.add_argument("since")
    observe_refs_parser.add_argument("packages_file")
    observe_refs_parser.add_argument("observed_at", type=int)
    refresh_parser = package_commands.add_parser(
        "refresh",
        help="refresh and publish one package",
    )
    _add_package_arguments(refresh_parser)
    refresh_parser.add_argument("legacy_table")
    refresh_parser.add_argument("since")
    refresh_parser.add_argument("write_legacy", choices=("true", "false"))
    refresh_parser.add_argument("use_rest_api", choices=("true", "false"))
    refresh_parser.add_argument("fast_out", choices=("true", "false"))


def _add_owner_parsers(subparsers: Any) -> None:
    owner_parser = subparsers.add_parser(
        "owner",
        help="run migrated owner update operations",
    )
    owner_commands = owner_parser.add_subparsers(
        dest="owner_command",
        required=True,
    )
    refresh_plan_parser = owner_commands.add_parser(
        "refresh-plan",
        help="print current-batch direct package work for one owner",
    )
    refresh_plan_parser.add_argument("owner_id")
    refresh_plan_parser.add_argument("owner")
    refresh_plan_parser.add_argument("since")
    refresh_packages_parser = owner_commands.add_parser(
        "refresh-packages",
        help="refresh newline-delimited owner package refs from stdin",
    )
    refresh_packages_parser.add_argument("owner_id")
    refresh_packages_parser.add_argument("owner_type", choices=("orgs", "users"))
    refresh_packages_parser.add_argument("owner")
    refresh_packages_parser.add_argument("since")
    refresh_packages_parser.add_argument("fast_out", choices=("true", "false"))
    scan_pages_parser = owner_commands.add_parser(
        "scan-pages",
        help="fetch, refresh, and advance one bounded owner listing pass",
    )
    scan_pages_parser.add_argument("owner_id")
    scan_pages_parser.add_argument("owner_type", choices=("orgs", "users"))
    scan_pages_parser.add_argument("owner")
    scan_pages_parser.add_argument("marker")
    scan_pages_parser.add_argument("since")
    scan_pages_parser.add_argument("start_page", type=int)
    scan_pages_parser.add_argument("fast_out", choices=("true", "false"))
    scan_pages_parser.add_argument("result_file")
    verify_parser = owner_commands.add_parser(
        "verify-scan",
        help="verify known packages absent from one complete owner listing",
    )
    verify_parser.add_argument("owner_id")
    verify_parser.add_argument("owner")
    verify_parser.add_argument("marker")
    verify_parser.add_argument("since")
    verify_parser.add_argument("observed_at", type=int)
    publish_parser = owner_commands.add_parser(
        "publish",
        help="publish owner and repository aggregates from the database",
    )
    publish_parser.add_argument("owner_id")
    publish_parser.add_argument("owner")
    update_parser = owner_commands.add_parser(
        "update",
        help="run one resumable owner update lifecycle",
    )
    update_parser.add_argument("owner_id")
    update_parser.add_argument("owner")
    update_parser.add_argument("since")
    update_parser.add_argument("batch_marker")
    update_parser.add_argument("fast_out", choices=("true", "false"))
    update_parser.add_argument("result_file")


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


def _add_discovery_parsers(subparsers: Any) -> None:
    discovery_parser = subparsers.add_parser(
        "discovery",
        help="run migrated owner discovery operations",
    )
    discovery_commands = discovery_parser.add_subparsers(
        dest="discovery_command",
        required=True,
    )
    owner_type_parser = discovery_commands.add_parser(
        "owner-type",
        help="print GitHub's owner typename for one login",
    )
    owner_type_parser.add_argument("owner")
    resolve_owner_parser = discovery_commands.add_parser(
        "resolve-owner",
        help="resolve one owner login to an ID/login reference",
    )
    resolve_owner_parser.add_argument("owner")
    resolve_owners_parser = discovery_commands.add_parser(
        "resolve-owner-ids",
        help="resolve owner candidates from a file",
    )
    resolve_owners_parser.add_argument("candidates_file")
    resolve_owners_parser.add_argument("missing_file", nargs="?")
    repo_nodes_parser = discovery_commands.add_parser(
        "repo-nodes",
        help="print one repository GraphQL discovery page",
    )
    repo_nodes_parser.add_argument("owner")
    repo_nodes_parser.add_argument("repo")
    repo_nodes_parser.add_argument("edge")
    repo_nodes_parser.add_argument("cursor", nargs="?", default="")
    owner_nodes_parser = discovery_commands.add_parser(
        "owner-nodes",
        help="print one owner GraphQL discovery page",
    )
    owner_nodes_parser.add_argument("owner")
    owner_nodes_parser.add_argument("edge")
    owner_nodes_parser.add_argument("cursor", nargs="?", default="")
    owner_nodes_parser.add_argument("owner_type", nargs="?", default="")
    owner_page_parser = discovery_commands.add_parser(
        "owner-page",
        help="print one REST user and organization discovery page",
    )
    owner_page_parser.add_argument("page", type=int)
    owner_page_parser.add_argument("last_id", type=int)
    owner_page_parser.add_argument("per_page", type=int)
    admit_owner_page_parser = discovery_commands.add_parser(
        "admit-owner-page",
        help="fetch and admit one REST owner discovery page",
    )
    admit_owner_page_parser.add_argument("page", type=int)
    admit_owner_page_parser.add_argument("per_page", type=int)
    admit_owner_page_parser.add_argument("packages_all_file")
    orgs_parser = discovery_commands.add_parser(
        "orgs",
        help="print one user's organization discovery results",
    )
    orgs_parser.add_argument("owner")
    orgs_parser.add_argument("--resolve", action="store_true")
    explore_parser = discovery_commands.add_parser(
        "explore",
        help="print authenticated connection discovery results",
    )
    explore_parser.add_argument("node")
    explore_parser.add_argument("edge", nargs="?", default="")
    membership_parser = discovery_commands.add_parser(
        "membership",
        help="print authenticated owner membership discovery results",
    )
    membership_parser.add_argument("owner")


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
