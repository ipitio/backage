"""Generate the supported runtime surface from authoritative names."""

from __future__ import annotations

import argparse
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from .database.schema import ensure
from .runtime_names import EnvironmentVariable, RunFile, StateKey, StatePrefix


@dataclass(frozen=True)
class _EnvironmentReference:
    variable: EnvironmentVariable
    scope: str
    owners: tuple[str, ...]
    secret: bool = False

    def as_dict(self) -> dict[str, object]:
        """Return one JSON-compatible environment reference."""

        return {
            "name": self.variable.value,
            "scope": self.scope,
            "owners": self.owners,
            "secret": self.secret,
        }


@dataclass(frozen=True)
class _StateReference:
    key: StateKey
    readers: tuple[str, ...]
    writers: tuple[str, ...]
    lifecycle: str = "active"

    def as_dict(self) -> dict[str, object]:
        """Return one JSON-compatible exact state-key reference."""

        return {
            "name": self.key.value,
            "readers": self.readers,
            "writers": self.writers,
            "lifecycle": self.lifecycle,
        }


@dataclass(frozen=True)
class _StatePrefixReference:
    prefix: StatePrefix
    owners: tuple[str, ...]
    lifecycle: str

    def as_dict(self) -> dict[str, object]:
        """Return one JSON-compatible state-family reference."""

        return {
            "pattern": f"{self.prefix.value}*",
            "owners": self.owners,
            "lifecycle": self.lifecycle,
        }


@dataclass(frozen=True)
class _PathReference:
    path: str
    owner: str
    lifecycle: str

    def as_dict(self) -> dict[str, str]:
        """Return one JSON-compatible path reference."""

        return {
            "path": self.path,
            "owner": self.owner,
            "lifecycle": self.lifecycle,
        }


def _environment(
    scope: str,
    owners: tuple[str, ...],
    *variables: EnvironmentVariable,
    secret: bool = False,
) -> tuple[_EnvironmentReference, ...]:
    return tuple(
        _EnvironmentReference(variable, scope, owners, secret) for variable in variables
    )


_ENVIRONMENT_REFERENCES = (
    *_environment(
        "credential",
        ("GitHubSettings", "WorkspaceSettings"),
        EnvironmentVariable.GITHUB_TOKEN,
        secret=True,
    ),
    *_environment(
        "credential",
        ("WorkspaceSettings",),
        EnvironmentVariable.GH_TOKEN,
        secret=True,
    ),
    *_environment(
        "repository",
        ("RepositoryIdentity", "RepositoryMaintenanceSettings"),
        EnvironmentVariable.GITHUB_OWNER,
        EnvironmentVariable.GITHUB_REPO,
    ),
    *_environment(
        "repository",
        ("RepositoryIdentity", "WorkspaceLayout"),
        EnvironmentVariable.GITHUB_BRANCH,
    ),
    *_environment(
        "identity",
        ("WorkspaceSettings", "HandoffSettings"),
        EnvironmentVariable.GITHUB_ACTOR,
        EnvironmentVariable.GITHUB_RUN_ID,
    ),
    *_environment(
        "handoff",
        ("HandoffSettings",),
        EnvironmentVariable.BKG_HANDOFF_CONTROL_REF,
        EnvironmentVariable.BKG_HANDOFF_POLL_SECONDS,
        EnvironmentVariable.BKG_HANDOFF_GIT_TIMEOUT_SECONDS,
    ),
    *_environment(
        "paths",
        ("RuntimeConfig", "UpdateWorkflowService"),
        EnvironmentVariable.BKG_ROOT,
        EnvironmentVariable.BKG_ENV,
        EnvironmentVariable.BKG_OWNERS,
        EnvironmentVariable.BKG_OPTOUT,
        EnvironmentVariable.BKG_INDEX,
        EnvironmentVariable.BKG_INDEX_DB,
        EnvironmentVariable.BKG_INDEX_SQL,
        EnvironmentVariable.BKG_INDEX_DIR,
    ),
    *_environment(
        "paths",
        ("RuntimeConfig", "OwnerIdentityCache"),
        EnvironmentVariable.BKG_OWNER_ID_CACHE,
    ),
    *_environment(
        "database-compatibility",
        ("RuntimeConfig", "DatabaseSettings"),
        EnvironmentVariable.BKG_INDEX_TBL_OWN,
        EnvironmentVariable.BKG_INDEX_TBL_PKG,
        EnvironmentVariable.BKG_INDEX_TBL_VER,
    ),
    *_environment(
        "runtime",
        ("RuntimeConfig",),
        EnvironmentVariable.BKG_MODE,
        EnvironmentVariable.BKG_MAX_LEN,
        EnvironmentVariable.BKG_IS_FIRST,
    ),
    *_environment(
        "version-selection",
        ("RuntimeConfig", "VersionSelectionSettings"),
        EnvironmentVariable.BKG_MAX_VERSION_PAGES,
        EnvironmentVariable.BKG_TAG_CACHE_PAGES,
        EnvironmentVariable.BKG_APPEND_TAGGED_VERSIONS_LIMIT,
    ),
    *_environment(
        "discovery",
        ("RuntimeConfig",),
        EnvironmentVariable.BKG_OWNER_DISCOVERY_MAX_PAGES,
    ),
    *_environment(
        "snapshot",
        ("RuntimeConfig", "SnapshotStore"),
        EnvironmentVariable.BKG_SNAPSHOT_ROTATION_THRESHOLD_BYTES,
    ),
    *_environment(
        "concurrency",
        ("RuntimeConfig", "ConcurrencySettings"),
        EnvironmentVariable.BKG_PARALLEL_ASYNC_MAX_JOBS,
        EnvironmentVariable.BKG_OWNER_UPDATE_STOP_GRACE,
    ),
    *_environment(
        "docker-size-fallback",
        ("RuntimeConfig", "DockerSizeInspector"),
        EnvironmentVariable.BKG_DOCKER_SIZE_FALLBACK,
        EnvironmentVariable.BKG_DOCKER_PLATFORM,
        EnvironmentVariable.BKG_DOCKER_PULL_TIMEOUT,
        EnvironmentVariable.BKG_DOCKER_COMMAND_TIMEOUT,
    ),
    *_environment(
        "github-http",
        ("GitHubSettings",),
        EnvironmentVariable.BKG_GITHUB_API_URL,
        EnvironmentVariable.BKG_HTTP_CONNECT_TIMEOUT,
        EnvironmentVariable.BKG_HTTP_READ_TIMEOUT,
        EnvironmentVariable.BKG_HTTP_WRITE_TIMEOUT,
        EnvironmentVariable.BKG_HTTP_POOL_TIMEOUT,
        EnvironmentVariable.BKG_HTTP_TOTAL_TIMEOUT,
        EnvironmentVariable.BKG_HTTP_MAX_ATTEMPTS,
        EnvironmentVariable.BKG_HTTP_INITIAL_BACKOFF,
        EnvironmentVariable.BKG_HTTP_MAX_BACKOFF,
        EnvironmentVariable.BKG_GITHUB_REST_RESERVE,
        EnvironmentVariable.BKG_HTTP_USER_AGENT,
    ),
    *_environment(
        "sqlite",
        ("DatabaseTuning",),
        EnvironmentVariable.BKG_SQLITE_BUSY_TIMEOUT_MS,
        EnvironmentVariable.BKG_SQLITE_MAX_ATTEMPTS,
        EnvironmentVariable.BKG_SQLITE_RETRY_DELAY_SECS,
    ),
    *_environment(
        "owner-retry",
        ("DatabaseTuning",),
        EnvironmentVariable.BKG_OWNER_RETRY_INITIAL_SECONDS,
        EnvironmentVariable.BKG_OWNER_RETRY_MAX_SECONDS,
    ),
    *_environment(
        "aggregate",
        ("AggregateSettings",),
        EnvironmentVariable.BKG_OWNER_ARRAY_MAX_BYTES,
        EnvironmentVariable.BKG_OWNER_ARRAY_ADAPTIVE_MAX_PROBE,
        EnvironmentVariable.BKG_OWNER_ARRAY_DB_ESTIMATE_HEADROOM_PERCENT,
        EnvironmentVariable.BKG_OWNER_ARRAY_DB_FALLBACK_VERSION_LIMIT,
        EnvironmentVariable.BKG_OWNER_ARRAY_VERSION_LIMIT,
        EnvironmentVariable.BKG_OWNER_ARRAY_DB_VERSION_LIMIT,
    ),
    *_environment(
        "publication",
        ("PublicationLimits",),
        EnvironmentVariable.BKG_JSON_XML_MAX_BYTES,
        EnvironmentVariable.BKG_JSON_XML_HARD_MAX_BYTES,
    ),
)


_STATE_REFERENCES = (
    _StateReference(
        StateKey.BATCH_FIRST_STARTED,
        ("BatchRuntimeService", "RunCoordinator", "RunApplicationOperations"),
        ("BatchRuntimeService",),
    ),
    _StateReference(
        StateKey.BATCH_MARKER,
        ("BatchRuntimeService", "RunCoordinator", "RunApplicationOperations"),
        ("BatchRuntimeService",),
    ),
    _StateReference(
        StateKey.PACKAGE_PROGRESS_MARKER,
        ("RunStartupService",),
        ("BatchRuntimeService", "RunStartupService"),
    ),
    _StateReference(
        StateKey.CALLS_TO_API,
        ("BatchRuntimeService",),
        ("BatchRuntimeService", "GitHubRateAccounting"),
    ),
    _StateReference(
        StateKey.DIFF,
        ("BatchRuntimeService",),
        ("BatchRuntimeService", "RunCoordinator"),
    ),
    _StateReference(
        StateKey.DISCOVERED_CONNECTION_OWNERS,
        ("OwnerLifecycleService",),
        ("BatchRuntimeService", "OwnerQueuePreparationService"),
    ),
    _StateReference(
        StateKey.GRAPHQL_LAST_COST,
        (),
        ("GitHubRateAccounting",),
    ),
    _StateReference(
        StateKey.GRAPHQL_REMAINING,
        (),
        ("GitHubRateAccounting",),
    ),
    _StateReference(
        StateKey.GRAPHQL_RESET_AT,
        (),
        ("GitHubRateAccounting",),
    ),
    _StateReference(
        StateKey.LAST_EXPLORE_DATE,
        ("BatchRuntimeService",),
        ("BatchRuntimeService",),
    ),
    _StateReference(
        StateKey.LAST_OWNERS_QUEUE_DATE,
        ("BatchRuntimeService",),
        ("BatchRuntimeService",),
    ),
    _StateReference(
        StateKey.LAST_SCANNED_ID,
        ("BatchRuntimeService", "admit_owner_page"),
        ("BatchRuntimeService", "admit_owner_page"),
    ),
    _StateReference(
        StateKey.MIN_CALLS_TO_API,
        ("BatchRuntimeService",),
        ("BatchRuntimeService", "GitHubRateAccounting"),
    ),
    _StateReference(
        StateKey.MIN_RATE_LIMIT_START,
        ("BatchRuntimeService",),
        ("BatchRuntimeService",),
    ),
    _StateReference(
        StateKey.OUT,
        ("RunStartupService",),
        ("RunFinalizationService",),
    ),
    _StateReference(
        StateKey.RATE_LIMIT_START,
        ("BatchRuntimeService",),
        ("BatchRuntimeService",),
    ),
    _StateReference(
        StateKey.REST_LIMIT,
        (),
        ("GitHubRateAccounting",),
    ),
    _StateReference(
        StateKey.REST_REMAINING,
        ("GitHubRateAccounting",),
        ("GitHubRateAccounting",),
    ),
    _StateReference(
        StateKey.REST_RESET_AT,
        ("GitHubRateAccounting",),
        ("GitHubRateAccounting",),
    ),
    _StateReference(
        StateKey.REST_TO_TOP,
        ("BatchRuntimeService", "RunCoordinator"),
        ("BatchRuntimeService", "RunCoordinator"),
    ),
    _StateReference(
        StateKey.SCRIPT_START,
        ("StopController",),
        ("BatchRuntimeService", "UpdateWorkflowService"),
    ),
    _StateReference(
        StateKey.TIMEOUT,
        ("StopController",),
        (
            "StopController",
            "BatchRuntimeService",
            "RunCoordinator",
            "UpdateWorkflowService",
        ),
        "transient",
    ),
    _StateReference(
        StateKey.LEGACY_OWNERS_QUEUE,
        ("RunStartupService",),
        ("RunStartupService", "RunPublicationService"),
        "compatibility-import",
    ),
    _StateReference(
        StateKey.OBSOLETE_PAGE_ALL,
        (),
        ("RunPublicationService",),
        "obsolete-delete",
    ),
    _StateReference(
        StateKey.OBSOLETE_INDEX_CLEANUP_DONE,
        (),
        ("RunPublicationService",),
        "obsolete-delete",
    ),
)


_STATE_PREFIX_REFERENCES = (
    _StatePrefixReference(
        StatePrefix.LEGACY_VERSIONS,
        ("RunPublicationService",),
        "obsolete-delete",
    ),
    _StatePrefixReference(
        StatePrefix.LEGACY_PACKAGES,
        ("RunPublicationService",),
        "obsolete-delete",
    ),
    _StatePrefixReference(
        StatePrefix.LEGACY_OWNER_SCAN,
        ("OwnerLifecycleService", "RunPublicationService"),
        "compatibility-import-delete",
    ),
    _StatePrefixReference(
        StatePrefix.LEGACY_OWNER_PAGE,
        ("OwnerLifecycleService", "RunPublicationService"),
        "compatibility-import-delete",
    ),
    _StatePrefixReference(
        StatePrefix.LEGACY_OWNER_SCRATCH,
        ("RunPublicationService",),
        "obsolete-delete",
    ),
)


_PATH_REFERENCES = (
    _PathReference("${BKG_ROOT}/owners.txt", "RuntimeConfig", "source-input"),
    _PathReference("${BKG_ROOT}/optout.txt", "RuntimeConfig", "source-input"),
    _PathReference("${BKG_ROOT}/README.md", "RunPublicationService", "generated"),
    _PathReference("${BKG_ROOT}/src/env.env", "StateStore", "working-state"),
    _PathReference(
        "${BKG_ROOT}/src/owner-id-cache.txt",
        "OwnerIdentityCache",
        "working-cache",
    ),
    _PathReference("${BKG_ROOT}/${BKG_INDEX}.db", "SnapshotStore", "working"),
    _PathReference(
        "${BKG_ROOT}/${BKG_INDEX}.sql",
        "SnapshotStore",
        "restore-compatibility",
    ),
    _PathReference("${BKG_ROOT}/${BKG_INDEX}", "WorkspaceLayout", "index-worktree"),
    _PathReference(
        "${BKG_ROOT}/.snapshot/${BKG_INDEX}.db",
        "SnapshotStore",
        "release-asset",
    ),
    _PathReference(
        "${BKG_ROOT}/${BKG_INDEX}.db.snapshot.sha256",
        "SnapshotStore",
        "working-signature",
    ),
    _PathReference(
        "${BKG_ROOT}/${BKG_INDEX}.db.zst",
        "SnapshotStore",
        "restore-compatibility",
    ),
    _PathReference(
        "${BKG_ROOT}/${BKG_INDEX}.sql.zst",
        "SnapshotStore",
        "restore-compatibility",
    ),
    *(
        _PathReference(
            f"${{BKG_ROOT}}/src/{name}",
            "PackageWorkPlanService",
            "run-intermediate",
        )
        for name in (
            RunFile.PACKAGES_ALL,
            RunFile.ALL_OWNERS_IN_DB,
            RunFile.OWNERS_PARTIALLY_UPDATED,
            RunFile.OWNERS_STALE,
            RunFile.OWNERS_SCANNED_WITHOUT_PACKAGES,
        )
    ),
    *(
        _PathReference(
            f"${{BKG_ROOT}}/src/{name}",
            "RunPublicationService",
            "obsolete-delete",
        )
        for name in (
            RunFile.LEGACY_PACKAGES_ALREADY_UPDATED,
            RunFile.LEGACY_PACKAGES_TO_UPDATE,
            RunFile.LEGACY_ALL_OWNERS_TO_UPDATE,
            RunFile.LEGACY_OWNERS_UPDATED,
            RunFile.LEGACY_OWNERS_DEFERRED,
        )
    ),
    _PathReference("${BKG_INDEX_DIR}/.env", "UpdateWorkspacePublisher", "generated"),
    _PathReference("${BKG_INDEX_DIR}/.nojekyll", "WorkspaceLayout", "generated"),
    _PathReference("${BKG_INDEX_DIR}/.json|.xml", "RunPublicationService", "generated"),
    _PathReference(
        "${BKG_INDEX_DIR}/dashboard.json",
        "publish_dashboard",
        "generated",
    ),
    _PathReference(
        "${BKG_INDEX_DIR}/dashboard-history.json",
        "publish_dashboard",
        "generated-history",
    ),
    _PathReference(
        "${BKG_INDEX_DIR}/.bkg-site-manifest.json",
        "publish_site_shell",
        "generated",
    ),
    _PathReference("${BKG_INDEX_DIR}/index.html", "publish_site_shell", "generated"),
    _PathReference(
        "${BKG_INDEX_DIR}/.bkg-site/assets/*",
        "publish_site_shell",
        "generated",
    ),
    _PathReference("${BKG_INDEX_DIR}/README.md", "RunPublicationService", "generated"),
    _PathReference(
        "${BKG_INDEX_DIR}/logo-b.webp|favicon.ico",
        "RunPublicationService",
        "generated",
    ),
    _PathReference("${BKG_ROOT}/CHANGELOG.md", "RunPublicationService", "generated"),
    _PathReference(
        "${BKG_INDEX_DIR}/{owner}/.json|.xml",
        "OwnerPublicationService",
        "generated",
    ),
    _PathReference(
        "${BKG_INDEX_DIR}/{owner}/{repo}/.json|.xml",
        "OwnerPublicationService",
        "generated",
    ),
    _PathReference(
        "${BKG_INDEX_DIR}/{owner}/{repo}/{package}.json|.xml",
        "PackageRefreshService",
        "generated",
    ),
)


def build_runtime_reference(
    parser: argparse.ArgumentParser,
) -> dict[str, object]:
    """Return one structured inventory of bkg's supported runtime surface."""

    _validate_reference_coverage()
    return {
        "schema_version": 1,
        "environment": [
            reference.as_dict()
            for reference in sorted(
                _ENVIRONMENT_REFERENCES,
                key=lambda item: item.variable.value,
            )
        ],
        "cli": _cli_reference(parser),
        "state": [reference.as_dict() for reference in _STATE_REFERENCES],
        "state_patterns": [
            reference.as_dict() for reference in _STATE_PREFIX_REFERENCES
        ],
        "database_schema": _database_schema_reference(),
        "database_compatibility_patterns": (
            "${BKG_INDEX_TBL_PKG}",
            "${BKG_INDEX_TBL_VER}",
            "${BKG_INDEX_TBL_VER}_{owner_type}_{package_type}_{owner}_{repo}_{package}",
        ),
        "paths": [
            reference.as_dict()
            for reference in sorted(_PATH_REFERENCES, key=lambda item: item.path)
        ],
    }


def _validate_reference_coverage() -> None:
    referenced_environment = [item.variable for item in _ENVIRONMENT_REFERENCES]
    if len(referenced_environment) != len(set(referenced_environment)):
        raise RuntimeError("environment reference contains duplicate names")
    if set(referenced_environment) != set(EnvironmentVariable):
        raise RuntimeError("environment reference does not cover every variable")

    referenced_state = [item.key for item in _STATE_REFERENCES]
    if len(referenced_state) != len(set(referenced_state)):
        raise RuntimeError("state reference contains duplicate names")
    if set(referenced_state) != set(StateKey):
        raise RuntimeError("state reference does not cover every exact key")

    referenced_prefixes = [item.prefix for item in _STATE_PREFIX_REFERENCES]
    if len(referenced_prefixes) != len(set(referenced_prefixes)):
        raise RuntimeError("state reference contains duplicate patterns")
    if set(referenced_prefixes) != set(StatePrefix):
        raise RuntimeError("state reference does not cover every key pattern")

    run_file_counts = {
        name: sum(
            reference.path == f"${{BKG_ROOT}}/src/{name}"
            for reference in _PATH_REFERENCES
        )
        for name in RunFile
    }
    if any(count != 1 for count in run_file_counts.values()):
        raise RuntimeError("path reference must cover every run file exactly once")


def _cli_reference(parser: argparse.ArgumentParser) -> list[dict[str, object]]:
    commands: list[dict[str, object]] = []

    def visit(current: argparse.ArgumentParser, path: tuple[str, ...]) -> None:
        arguments: list[dict[str, object]] = []
        children: dict[str, argparse.ArgumentParser] = {}
        for action in current._actions:  # pylint: disable=protected-access
            subparsers = _subparser_choices(action)
            if subparsers is not None:
                children.update(subparsers)
                continue
            arguments.append(_argument_reference(action))
        commands.append(
            {
                "command": " ".join(("bkg", *path)),
                "arguments": arguments,
            }
        )
        for name, child in sorted(children.items()):
            visit(child, (*path, name))

    visit(parser, ())
    return commands


def _subparser_choices(
    action: argparse.Action,
) -> dict[str, argparse.ArgumentParser] | None:
    candidate = cast(object, action.choices)
    if not isinstance(candidate, Mapping):
        return None
    choices = cast(Mapping[object, object], candidate)
    if not choices or not all(
        isinstance(name, str) and isinstance(value, argparse.ArgumentParser)
        for name, value in choices.items()
    ):
        return None
    return {
        cast(str, name): cast(argparse.ArgumentParser, value)
        for name, value in choices.items()
    }


def _argument_reference(action: argparse.Action) -> dict[str, object]:
    choices = action.choices
    return {
        "name": action.dest,
        "forms": tuple(action.option_strings) or (action.dest,),
        "kind": "option" if action.option_strings else "positional",
        "required": action.required,
        "nargs": action.nargs,
        "default": _json_compatible(action.default),
        "choices": (
            ()
            if choices is None
            else tuple(str(value) for value in cast(Iterable[object], choices))
        ),
    }


def _json_compatible(value: object) -> object:
    if value is None or isinstance(value, (bool, float, int, str)):
        return value
    if isinstance(value, Path):
        return str(value)
    return repr(value)


def _database_schema_reference() -> list[dict[str, str]]:
    """Materialize and inspect the current lazy schema without a disk file."""

    with sqlite3.connect(":memory:") as connection:
        ensure(connection, "owners", "packages", "versions")
        rows = connection.execute(
            """
            select type, name, tbl_name
            from sqlite_schema
            where name not like 'sqlite_%'
            order by type, name
            """
        ).fetchall()
    return [
        {
            "type": str(object_type),
            "name": str(name),
            "table": str(table),
            "owner": "database.schema",
        }
        for object_type, name, table in rows
    ]
