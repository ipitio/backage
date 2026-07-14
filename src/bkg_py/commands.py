"""Execute shell-compatible bkg Python commands."""
# pylint: disable=import-outside-toplevel

from __future__ import annotations

import argparse
import base64
import json
import sys
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import TYPE_CHECKING

from .result import ExitStatus
from .versions import (
    VersionListingContext,
    extract_embedded_manifest,
    extract_version_page_data,
    manifest_size,
    parse_version_listing_html,
    version_cache_records,
)

if TYPE_CHECKING:
    from .application import ApplicationContext
    from .database import (
        DatabaseRepository,
        OwnerScanResult,
        PackageRef,
    )
    from .discovery import (
        DiscoveryPage,
        OwnerIdentityResolver,
        RestOwnerDiscoveryPage,
    )
    from .owners import OwnerPageAdmissionResult
    from .runtime import GracefulStop
    from .snapshots import SnapshotStore
    from .version_updates import VersionRefreshResult


def _package_ref(args: argparse.Namespace) -> PackageRef:
    from .database import PackageRef

    return PackageRef(
        owner_id=args.owner_id,
        owner_type=args.owner_type,
        package_type=args.package_type,
        owner=args.owner,
        repo=args.repo,
        package=args.package,
    )


def _optional_argument(value: str) -> str | None:
    return None if value == "-" else value


def _boolean_argument(value: str) -> bool:
    return value == "true"


def _print_owner_scan_result(result: OwnerScanResult) -> None:
    print(
        json.dumps(
            {
                "removed": [
                    {
                        "owner_type": package.owner_type,
                        "package_type": package.package_type,
                        "repo": package.repo,
                        "package": package.package,
                    }
                    for package in result.removed
                ],
                "pending": [
                    {
                        "owner_type": package.owner_type,
                        "package_type": package.package_type,
                        "repo": package.repo,
                        "package": package.package,
                    }
                    for package in result.pending
                ],
                "pending_count": result.pending_count,
                "retry_after": result.retry_after,
            },
            separators=(",", ":"),
        )
    )


def _graceful_stop_status(error: GracefulStop) -> ExitStatus:
    reason = str(error) or "requested"
    print(f"Graceful stop requested: {reason}", file=sys.stderr)
    return ExitStatus.GRACEFUL_STOP


def _run_publication(
    args: argparse.Namespace,
    application: ApplicationContext,
) -> ExitStatus:
    from .publication import PublicationError, publish_json_file, write_xml_file
    from .runtime import GracefulStop

    try:
        with application.stop.signal_handlers():
            if args.command == "publish":
                publish_json_file(
                    Path(args.file),
                    application.stop.check,
                    application.publication_limits,
                )
            else:
                print(write_xml_file(Path(args.file), application.stop.check))
    except GracefulStop as error:
        return _graceful_stop_status(error)
    except (OSError, PublicationError) as error:
        print(error, file=sys.stderr)
        return ExitStatus.NON_FATAL
    return ExitStatus.SUCCESS


def _run_database(
    args: argparse.Namespace,
    application: ApplicationContext,
) -> ExitStatus:
    from .database import DatabaseError, VersionStage
    from .runtime import GracefulStop

    try:
        with application.stop.signal_handlers():
            if args.database_command == "ensure-schema":
                application.database.ensure_schema()
            elif args.database_command == "flush-version-stage":
                application.database.flush_version_stage(
                    VersionStage.load(Path(args.directory))
                )
            elif args.database_command == "cleanup-legacy-package":
                application.database.cleanup_legacy_package(
                    _package_ref(args),
                    args.legacy_table,
                    since=args.since,
                )
            elif args.database_command == "cleanup-legacy-all":
                application.database.cleanup_replaced_legacy_tables(since=args.since)
            elif args.database_command == "retire-owner":
                application.database.retire_owner(args.owner)
            elif _run_owner_scan_database(args, application.database):
                pass
            else:
                raise DatabaseError(
                    f"unknown database command: {args.database_command}"
                )
    except GracefulStop as error:
        return _graceful_stop_status(error)
    except (DatabaseError, OSError) as error:
        print(error, file=sys.stderr)
        return ExitStatus.NON_FATAL
    return ExitStatus.SUCCESS


def _run_owner_scan_database(
    args: argparse.Namespace,
    database: DatabaseRepository,
) -> bool:
    return _run_owner_scan_lifecycle(args, database) or _run_owner_scan_retry(
        args,
        database,
    )


def _run_owner_scan_lifecycle(
    args: argparse.Namespace,
    database: DatabaseRepository,
) -> bool:
    from .database import DatabaseError, load_owner_scan_packages

    command = args.database_command
    if command == "begin-owner-scan":
        database.begin_owner_scan(
            args.owner_id,
            args.owner,
            args.marker,
            args.started_at,
        )
    elif command == "active-owner-scan":
        if not database.owner_scan_active(args.owner_id, args.marker):
            raise DatabaseError(
                f"owner scan {args.owner_id}/{args.marker} is not active"
            )
    elif command == "observe-owner-scan":
        database.observe_owner_scan(
            args.owner_id,
            args.marker,
            load_owner_scan_packages(Path(args.packages_file)),
            args.observed_at,
        )
    elif command == "missing-owner-scan-packages":
        for package in database.missing_owner_scan_packages(
            args.owner_id,
            args.marker,
        ):
            print(
                package.owner_type,
                package.package_type,
                package.repo,
                package.package,
                sep="\t",
            )
    elif command == "complete-owner-scan":
        _print_owner_scan_result(
            database.complete_owner_scan(
                args.owner_id,
                args.marker,
                args.scan_date,
                args.completed_at,
            )
        )
    else:
        return False
    return True


def _run_owner_scan_retry(
    args: argparse.Namespace,
    database: DatabaseRepository,
) -> bool:
    from .database import OwnerScanFailure

    command = args.database_command
    if command == "fail-owner-scan":
        print(
            database.fail_owner_scan(
                OwnerScanFailure(
                    args.owner_id,
                    args.owner,
                    _optional_argument(args.marker),
                    args.error,
                    args.failed_at,
                )
            )
        )
    elif command == "clear-owner-backoff":
        database.clear_owner_backoff(
            args.owner_id,
            args.owner,
            args.completed_at,
        )
    elif command == "deferred-owners":
        for owner, retry_after in database.deferred_owners(args.now):
            print(owner, retry_after, sep="\t")
    else:
        return False
    return True


def _run_render(
    args: argparse.Namespace,
    application: ApplicationContext,
) -> ExitStatus:
    from . import rendering
    from .database import DatabaseError
    from .runtime import GracefulStop

    try:
        with application.stop.signal_handlers():
            if args.render_command == "aggregate-files":
                rendering.render_file_aggregate(
                    Path(args.source_directory),
                    Path(args.destination),
                    settings=application.aggregate_settings,
                    check_stop=application.stop.check,
                )
                return ExitStatus.SUCCESS

            if args.render_command == "versions":
                package = _package_ref(args)
                snapshot = application.database.package_snapshot(
                    package,
                    since=args.since,
                    legacy_table=_optional_argument(args.legacy_table),
                )
                if snapshot is None:
                    raise rendering.RenderingError(
                        f"no package row found for {package.owner}/{package.package}"
                    )
                value = rendering.render_version_array(
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
                has_versions = rendering.render_package_file(
                    application.database,
                    package,
                    Path(args.destination),
                    rendering.PackageRenderOptions(
                        since=args.since,
                        output_date=_optional_argument(args.output_date),
                        version_limit=args.version_limit,
                        legacy_table=_optional_argument(args.legacy_table),
                    ),
                    application.stop.check,
                )
                if not has_versions:
                    print(
                        f"No version rows available for "
                        f"{package.owner}/{package.package}; "
                        "using package-level fallback data",
                        file=sys.stderr,
                    )
            elif args.render_command == "aggregate-database":
                rendering.render_database_aggregate(
                    application.database,
                    args.owner_id,
                    Path(args.destination),
                    rendering.DatabaseAggregateOptions(
                        repo=_optional_argument(args.repo),
                        size_hint_directory=(
                            None
                            if args.size_hint_directory == "-"
                            else Path(args.size_hint_directory)
                        ),
                        settings=application.aggregate_settings,
                    ),
                    application.stop.check,
                )
            elif args.render_command == "repositories":
                sys.stdout.writelines(
                    f"{repo}\n"
                    for repo in application.database.repository_names(args.owner_id)
                )
            else:
                raise rendering.RenderingError(
                    f"unknown render command: {args.render_command}"
                )
    except GracefulStop as error:
        return _graceful_stop_status(error)
    except (DatabaseError, OSError, rendering.RenderingError) as error:
        print(error, file=sys.stderr)
        return ExitStatus.NON_FATAL
    return ExitStatus.SUCCESS


def _run_github(
    args: argparse.Namespace,
    application: ApplicationContext,
) -> ExitStatus:
    from .github import GitHubError, dump_json
    from .runtime import GracefulStop

    try:
        with application.stop.signal_handlers(), application.github_client() as client:
            if args.github_command == "rest":
                response = (
                    client.rest_json_optional(args.path)
                    if args.missing_ok
                    else client.rest_json(args.path)
                )
                print(dump_json(None if response is None else response.value))
            elif args.github_command == "graphql":
                print(dump_json(client.graphql(sys.stdin.read()).value))
            elif args.github_command == "download":
                client.download(
                    args.url,
                    Path(args.destination),
                    authenticated=args.authenticated,
                )
            else:
                raise GitHubError(f"unknown GitHub command: {args.github_command}")
    except GracefulStop as error:
        return _graceful_stop_status(error)
    except (GitHubError, OSError) as error:
        print(error, file=sys.stderr)
        return ExitStatus.NON_FATAL
    return ExitStatus.SUCCESS


def _run_discovery(
    args: argparse.Namespace,
    application: ApplicationContext,
) -> ExitStatus:
    from .discovery import DiscoveryError, OwnerIdentityCache, OwnerIdentityResolver
    from .github import GitHubError
    from .runtime import GracefulStop

    try:
        with application.stop.signal_handlers(), application.github_client() as client:
            resolver = OwnerIdentityResolver(
                OwnerIdentityCache.from_config(application.config),
                client,
            )
            _run_discovery_command(args, resolver, application)
    except GracefulStop as error:
        return _graceful_stop_status(error)
    except (DiscoveryError, GitHubError, OSError) as error:
        print(error, file=sys.stderr)
        return ExitStatus.NON_FATAL
    return ExitStatus.SUCCESS


def _run_discovery_command(
    args: argparse.Namespace,
    resolver: OwnerIdentityResolver,
    application: ApplicationContext,
) -> None:
    from .discovery import DiscoveryError

    command = args.discovery_command
    if command in {"owner-type", "resolve-owner", "resolve-owner-ids"}:
        _run_discovery_identity_command(args, resolver)
    elif command in {"repo-nodes", "owner-nodes", "owner-page", "admit-owner-page"}:
        _run_discovery_page_command(args, resolver, application)
    elif command in {"orgs", "explore", "membership"}:
        _run_discovery_traversal_command(args, resolver)
    else:
        raise DiscoveryError(f"unknown discovery command: {command}")


def _run_discovery_identity_command(
    args: argparse.Namespace,
    resolver: OwnerIdentityResolver,
) -> None:
    from .discovery import DiscoveryError

    command = args.discovery_command
    if command == "owner-type":
        owner_type = resolver.owner_type(args.owner)
        if owner_type is not None:
            print(owner_type)
    elif command == "resolve-owner":
        result = resolver.resolve_owner(args.owner)
        if result.owner_ref is not None:
            print(result.owner_ref)
    elif command == "resolve-owner-ids":
        missing_file = None if args.missing_file is None else Path(args.missing_file)
        sys.stdout.writelines(
            f"{owner_ref}\n"
            for owner_ref in resolver.resolve_candidate_file(
                Path(args.candidates_file),
                missing_path=missing_file,
            )
        )
    else:
        raise DiscoveryError(f"unknown discovery identity command: {command}")


def _run_discovery_page_command(
    args: argparse.Namespace,
    resolver: OwnerIdentityResolver,
    application: ApplicationContext,
) -> None:
    from .discovery import DiscoveryError
    from .owners import (
        OwnerPageAdmissionConfig,
        admit_owner_page,
    )

    command = args.discovery_command
    if command == "repo-nodes":
        _print_discovery_page(
            resolver.repository_nodes(
                args.owner,
                args.repo,
                args.edge,
                args.cursor,
            )
        )
    elif command == "owner-nodes":
        _print_discovery_page(
            resolver.owner_nodes(
                args.owner,
                args.edge,
                args.cursor,
                args.owner_type,
            )
        )
    elif command == "owner-page":
        _print_rest_owner_page(
            resolver.owner_page(
                args.page,
                last_id=args.last_id,
                per_page=args.per_page,
            )
        )
    elif command == "admit-owner-page":
        _print_owner_page_admission(
            admit_owner_page(
                resolver,
                OwnerPageAdmissionConfig(
                    application.state,
                    Path(application.config.owners_file),
                    Path(args.packages_all_file),
                ),
                args.page,
                args.per_page,
            )
        )
    else:
        raise DiscoveryError(f"unknown discovery page command: {command}")


def _run_discovery_traversal_command(
    args: argparse.Namespace,
    resolver: OwnerIdentityResolver,
) -> None:
    from .discovery import DiscoveryError

    command = args.discovery_command
    if command == "orgs":
        _print_lines(
            resolver.organization_logins(
                args.owner,
                resolve=args.resolve,
            )
        )
    elif command == "explore":
        _print_lines(resolver.explore(args.node, args.edge))
    elif command == "membership":
        _print_lines(resolver.membership(args.owner))
    else:
        raise DiscoveryError(f"unknown discovery traversal command: {command}")


def _print_discovery_page(page: DiscoveryPage) -> None:
    print("has_next", str(page.has_next_page).lower(), sep="\t")
    print("end_cursor", page.end_cursor, sep="\t")
    sys.stdout.writelines(f"node\t{node}\n" for node in page.nodes)


def _print_rest_owner_page(page: RestOwnerDiscoveryPage) -> None:
    print("users_count", page.users_count, sep="\t")
    print("orgs_count", page.orgs_count, sep="\t")
    for owner in page.owners:
        encoded = base64.b64encode(
            json.dumps(
                owner,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode()
        ).decode()
        print("owner", encoded, sep="\t")


def _print_owner_page_admission(result: OwnerPageAdmissionResult) -> None:
    print("has_more", str(result.has_more).lower(), sep="\t")
    print("owners_count", result.owners_count, sep="\t")
    print("admitted_count", result.admitted_count, sep="\t")
    sys.stdout.writelines(f"requested\t{login}\n" for login in result.requested_logins)


def _print_lines(values: Iterable[str]) -> None:
    sys.stdout.writelines(f"{value}\n" for value in values)


def _run_snapshot(
    args: argparse.Namespace,
    application: ApplicationContext,
) -> ExitStatus:
    from .database import DatabaseError
    from .github import GitHubError
    from .runtime import GracefulStop
    from .snapshots import SnapshotError

    try:
        with application.stop.signal_handlers():
            if args.snapshot_command == "download-release":
                return _run_snapshot_release_download(args, application)
            if args.snapshot_command == "rotate-if-needed":
                return _run_snapshot_rotation(args, application)
            return _run_snapshot_command(args, application.snapshots)
    except GracefulStop as error:
        return _graceful_stop_status(error)
    except (DatabaseError, GitHubError, OSError, SnapshotError) as error:
        print(error, file=sys.stderr)
        return ExitStatus.NON_FATAL


def _run_snapshot_release_download(
    args: argparse.Namespace,
    application: ApplicationContext,
) -> ExitStatus:
    with application.github_client() as client:
        asset = application.snapshots.release_snapshot_asset(
            client,
            owner=application.config.github_owner,
            repo=application.config.github_repo,
            tag=args.tag,
        )
        if asset is None:
            print(
                "No supported database snapshot asset found in release", file=sys.stderr
            )
            return ExitStatus.NON_FATAL
        if args.check:
            print(asset.name)
            return ExitStatus.SUCCESS
        result = application.snapshots.download_release_snapshot(client, asset)
    print(result.message)
    return ExitStatus.SUCCESS


def _run_snapshot_rotation(
    args: argparse.Namespace,
    application: ApplicationContext,
) -> ExitStatus:
    result = application.snapshots.rotate_database_if_needed(
        lambda: application.database.cleanup_replaced_legacy_tables(
            since=args.since,
            prune_normalized=True,
            vacuum=True,
        ),
        threshold_bytes=args.threshold_bytes,
        date_stamp=args.date_stamp,
    )
    if result.rotated and result.archive is not None:
        print(result.archive)
    return ExitStatus.SUCCESS


def _run_snapshot_command(
    args: argparse.Namespace,
    snapshots: SnapshotStore,
) -> ExitStatus:
    command = args.snapshot_command
    if command == "current-archive":
        return _print_current_snapshot_archive(snapshots)
    if command == "restore-signature-matches":
        return _snapshot_signature_status(snapshots)
    if command == "restore-if-needed":
        result = snapshots.restore_database_if_needed()
        if result is not None:
            print(result.message)
    elif command == "restore-archive-if-needed":
        result = snapshots.restore_archive_path_if_needed(Path(args.archive))
        print(result.message)
    elif command == "write-restore-signature":
        snapshots.write_restore_signature()
    elif command == "checkpoint":
        snapshots.checkpoint_database()
    else:
        _run_snapshot_value_command(args, snapshots)
    return ExitStatus.SUCCESS


def _run_snapshot_value_command(
    args: argparse.Namespace,
    snapshots: SnapshotStore,
) -> None:
    from .snapshots import SnapshotError

    command = args.snapshot_command
    if command == "current-signature":
        print(snapshots.current_signature())
    elif command == "path":
        print(snapshots.archive_path(args.kind))
    elif command == "asset-name":
        print(snapshots.asset_name(args.kind))
    elif command == "prepare":
        print(snapshots.prepare_database_snapshot())
    else:
        raise SnapshotError(f"unknown snapshot command: {command}")


def _print_current_snapshot_archive(snapshots: SnapshotStore) -> ExitStatus:
    archive = snapshots.current_archive()
    if archive is None:
        print("No database snapshot archive found", file=sys.stderr)
        return ExitStatus.NON_FATAL
    print(archive.path)
    return ExitStatus.SUCCESS


def _snapshot_signature_status(snapshots: SnapshotStore) -> ExitStatus:
    return (
        ExitStatus.SUCCESS
        if snapshots.restore_signature_matches()
        else ExitStatus.NON_FATAL
    )


def _run_version(args: argparse.Namespace) -> ExitStatus:
    if args.version_command == "refresh-package":
        from .application import ApplicationContext

        return _run_version_refresh(args, ApplicationContext.from_env())
    status = ExitStatus.SUCCESS
    if args.version_command == "parse-page-html":
        entries = parse_version_listing_html(
            sys.stdin.read(),
            VersionListingContext(
                owner_type=args.owner_type,
                owner=args.owner,
                repo=args.repo,
                package_type=args.package_type,
                package=args.package,
            ),
        )
        print(
            json.dumps(
                [entry.json_object() for entry in entries],
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            )
        )
    elif args.version_command == "extract-embedded-manifest":
        manifest = extract_embedded_manifest(sys.stdin.read())
        print(manifest)
    elif args.version_command == "extract-page-data":
        page_data = extract_version_page_data(sys.stdin.read())
        print(
            json.dumps(
                page_data.json_object(),
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            )
        )
    elif args.version_command == "manifest-size":
        result = manifest_size(sys.stdin.read())
        if result.fallback_reason is not None:
            summary = result.diagnostic_summary or 'sample="<empty>"'
            print(
                "Container manifest size fallback for "
                f"{_manifest_size_context(args.context)}: "
                f"{result.fallback_reason}; {summary}",
                file=sys.stderr,
            )
        print(result.size)
    elif args.version_command == "cache-candidates":
        sys.stdout.writelines(
            f"{record.tsv_row()}\n"
            for record in version_cache_records(sys.stdin.read())
        )
    else:
        status = ExitStatus.NON_FATAL
    return status


def _run_version_refresh(
    args: argparse.Namespace,
    application: ApplicationContext,
) -> ExitStatus:
    from . import registry, version_updates
    from .database import DatabaseError
    from .github import GitHubError
    from .runtime import GracefulStop
    from .version_ingestion import VersionIngestionError

    def diagnostic(message: str) -> None:
        print(message, file=sys.stderr)

    try:
        with application.stop.signal_handlers(), application.github_client() as client:
            result = version_updates.VersionRefreshService(
                application.database,
                client,
                version_updates.VersionRefreshExecution(
                    application.worker_runner,
                    registry.GHCRManifestInspector(client, diagnostic=diagnostic),
                    diagnostic=diagnostic,
                    metric_enrichment=application.metric_enrichment,
                    hosted_size_inspector=registry.GHCRBadgeSizeInspector(
                        client,
                        application.metric_enrichment,
                        diagnostic=diagnostic,
                    ),
                ),
            ).refresh(
                version_updates.VersionRefreshRequest(
                    _package_ref(args),
                    args.legacy_table,
                    _boolean_argument(args.write_legacy),
                    version_updates.VersionRefreshPolicy(
                        use_rest_api=_boolean_argument(args.use_rest_api),
                        authenticate_html=_version_html_authentication(application),
                        allow_hosted_size_fallback=(
                            _hosted_size_fallback_allowed(application)
                        ),
                    ),
                    args.since,
                ),
                application.version_selection_settings,
            )
    except GracefulStop as error:
        return _graceful_stop_status(error)
    except (
        DatabaseError,
        GitHubError,
        OSError,
        VersionIngestionError,
        version_updates.VersionRefreshError,
    ) as error:
        print(error, file=sys.stderr)
        return ExitStatus.NON_FATAL
    _print_version_refresh_result(result)
    return ExitStatus.SUCCESS


def _version_html_authentication(application: ApplicationContext) -> bool:
    from .package_updates import html_authentication_required

    return html_authentication_required(
        bool(application.github_settings.token),
        application.config.mode,
    )


def _hosted_size_fallback_allowed(application: ApplicationContext) -> bool:
    from .package_updates import hosted_size_fallback_allowed

    return hosted_size_fallback_allowed(application.config.mode)


def _print_version_refresh_result(result: VersionRefreshResult) -> None:
    selection = result.selection
    print(
        json.dumps(
            {
                "selected_ids": list(selection.selected_ids),
                "candidate_count": len(selection.candidates),
                "records_written": result.records_written,
                "version_pages_read": selection.version_pages_read,
                "tag_pages_read": selection.tag_pages_read,
                "used_fallback": selection.used_fallback,
            },
            separators=(",", ":"),
        )
    )


def _manifest_size_context(context: str | None) -> str:
    if context is None:
        return "manifest"
    cleaned = context.replace("\n", " ").replace("\r", " ")[:200]
    return cleaned or "manifest"


def _run_application_command(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
) -> ExitStatus:
    from .application import ApplicationContext

    application = ApplicationContext.from_env()
    runner = _application_runner(args.command, parser)
    return runner(args, application)


def _application_runner(
    command: str,
    parser: argparse.ArgumentParser,
) -> Callable[[argparse.Namespace, ApplicationContext], ExitStatus]:
    simple_runners = {
        "publish": _run_publication,
        "json-to-xml": _run_publication,
        "database": _run_database,
        "render": _run_render,
        "snapshot": _run_snapshot,
        "github": _run_github,
        "discovery": _run_discovery,
    }
    runner = simple_runners.get(command)
    if runner is not None:
        return runner
    if command == "run":
        from .run.commands import run_application

        return run_application
    if command == "package":
        from .package_commands import run_package

        return run_package
    if command == "owner":
        from .owners.commands import run_owner

        return run_owner
    if command == "orchestration":
        from .orchestration_commands import run_orchestration

        return run_orchestration
    parser.error(f"unknown command: {command}")
    raise AssertionError("argparse.error must exit")


def run_command(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
) -> ExitStatus:
    """Execute one parsed command and return its shell-facing status."""

    status = ExitStatus.FAILURE
    if args.command == "config":
        from .config import RuntimeConfig

        print(json.dumps(RuntimeConfig.from_env().as_dict(), sort_keys=True))
        status = ExitStatus.SUCCESS
    elif args.command == "validate":
        from .validation import validate_generated_file

        status = validate_generated_file(args.file)
    elif args.command == "select-owners":
        from .owners import OwnerQueuePaths, OwnerQueueSelector

        selector = OwnerQueueSelector(
            rest_first=args.rest_first,
            request_limit=args.request_limit,
            current_owner=args.current_owner,
            paths=OwnerQueuePaths(
                connections_file=Path(args.connections_file),
                manual_file=Path(args.manual_file),
                index_dir=Path(args.index_dir),
                state_dir=Path.cwd(),
            ),
        )
        selected = selector.select_with_reasons()
        sys.stdout.writelines(f"{owner}\n" for owner, _reason in selected)
        if args.reasons_file:
            Path(args.reasons_file).write_text(
                "".join(
                    f"{owner.split('/', maxsplit=1)[-1]}\t{reason}\n"
                    for owner, reason in selected
                ),
                encoding="utf-8",
            )
        status = ExitStatus.SUCCESS
    elif args.command == "version":
        status = _run_version(args)
    else:
        status = _run_application_command(args, parser)
    return status
