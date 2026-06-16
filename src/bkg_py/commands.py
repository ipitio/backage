"""Execute shell-compatible bkg Python commands."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .application import ApplicationContext
from .config import RuntimeConfig
from .database import (
    DatabaseError,
    DatabaseRepository,
    OwnerScanFailure,
    OwnerScanPackage,
    OwnerScanResult,
    PackageRef,
    VersionStage,
)
from .github import GitHubError, dump_json
from .owner_queue import OwnerQueueSelector
from .publication import PublicationError, publish_json_file, write_xml_file
from .rendering import (
    DatabaseAggregateOptions,
    PackageRenderOptions,
    RenderingError,
    render_database_aggregate,
    render_file_aggregate,
    render_package_file,
    render_version_array,
)
from .result import ExitStatus
from .runtime import GracefulStop
from .snapshots import SnapshotError, SnapshotStore
from .validation import validate_generated_file


def _package_ref(args: argparse.Namespace) -> PackageRef:
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


_OWNER_SCAN_PACKAGE_FIELDS = 4


def _owner_scan_packages(path: Path) -> tuple[OwnerScanPackage, ...]:
    packages: list[OwnerScanPackage] = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        if not line:
            continue
        fields = line.split("\t")
        if len(fields) != _OWNER_SCAN_PACKAGE_FIELDS or not all(fields):
            raise DatabaseError(f"invalid owner scan package at {path}:{line_number}")
        packages.append(OwnerScanPackage(*fields))
    return tuple(packages)


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
                "pending_count": result.pending_count,
                "retry_after": result.retry_after,
            },
            separators=(",", ":"),
        )
    )


def _run_publication(
    args: argparse.Namespace,
    application: ApplicationContext,
) -> ExitStatus:
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
    except GracefulStop:
        return ExitStatus.GRACEFUL_STOP
    except (OSError, PublicationError) as error:
        print(error, file=sys.stderr)
        return ExitStatus.NON_FATAL
    return ExitStatus.SUCCESS


def _run_database(
    args: argparse.Namespace,
    application: ApplicationContext,
) -> ExitStatus:
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
    except GracefulStop:
        return ExitStatus.GRACEFUL_STOP
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
            _owner_scan_packages(Path(args.packages_file)),
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
    try:
        with application.stop.signal_handlers():
            if args.render_command == "aggregate-files":
                render_file_aggregate(
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
                    application.database,
                    package,
                    Path(args.destination),
                    PackageRenderOptions(
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
                render_database_aggregate(
                    application.database,
                    args.owner_id,
                    Path(args.destination),
                    DatabaseAggregateOptions(
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
                raise RenderingError(f"unknown render command: {args.render_command}")
    except GracefulStop:
        return ExitStatus.GRACEFUL_STOP
    except (DatabaseError, OSError, RenderingError) as error:
        print(error, file=sys.stderr)
        return ExitStatus.NON_FATAL
    return ExitStatus.SUCCESS


def _run_github(
    args: argparse.Namespace,
    application: ApplicationContext,
) -> ExitStatus:
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
    except GracefulStop:
        return ExitStatus.GRACEFUL_STOP
    except (GitHubError, OSError) as error:
        print(error, file=sys.stderr)
        return ExitStatus.NON_FATAL
    return ExitStatus.SUCCESS


def _run_snapshot(
    args: argparse.Namespace,
    application: ApplicationContext,
) -> ExitStatus:
    try:
        with application.stop.signal_handlers():
            return _run_snapshot_command(args, application.snapshots)
    except GracefulStop:
        return ExitStatus.GRACEFUL_STOP
    except (OSError, SnapshotError) as error:
        print(error, file=sys.stderr)
        return ExitStatus.NON_FATAL


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
        return ExitStatus.NON_FATAL
    print(archive.path)
    return ExitStatus.SUCCESS


def _snapshot_signature_status(snapshots: SnapshotStore) -> ExitStatus:
    return (
        ExitStatus.SUCCESS
        if snapshots.restore_signature_matches()
        else ExitStatus.NON_FATAL
    )


def run_command(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
) -> ExitStatus:
    """Execute one parsed command and return its shell-facing status."""

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
    else:
        application = ApplicationContext.from_env()
        if args.command in {"publish", "json-to-xml"}:
            status = _run_publication(args, application)
        elif args.command == "database":
            status = _run_database(args, application)
        elif args.command == "render":
            status = _run_render(args, application)
        elif args.command == "snapshot":
            status = _run_snapshot(args, application)
        elif args.command == "github":
            status = _run_github(args, application)
        else:
            parser.error(f"unknown command: {args.command}")
    return status
