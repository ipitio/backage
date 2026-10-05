"""Render package metadata and bounded owner aggregates."""

import json
import os
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO, cast

from ..config import read_int, read_optional_int
from ..database.models import (
    PackageRecord,
    PackageRef,
    PackageSnapshot,
    VersionLimitEstimate,
    VersionRecord,
    VersionSource,
)
from ..database.package.repository import PackageRepository
from ..files import atomic_path, atomic_text_output
from ..runtime_names import EnvironmentVariable as Env
from .artifacts import JsonValue
from .baseline import publication_baseline
from .formatting import human_metric, human_size
from .version_counts import refresh_version_counts, version_count_fields

StopCheck = Callable[[], None]


class RenderingError(ValueError):
    """Database or file data cannot be rendered into a generated endpoint."""


@dataclass(frozen=True)
class AggregateSettings:
    """Byte limits and adaptive-selection controls for owner aggregates."""

    target_bytes: int = 35_000_000
    maximum_probe: int = 65_536
    estimate_headroom_percent: int = 75
    database_fallback_version_limit: int = 2
    version_limit: int | None = None
    database_version_limit: int | None = None

    @classmethod
    def from_mapping(cls, values: Mapping[str, str]) -> AggregateSettings:
        """Read aggregate controls from captured configuration."""

        return cls(
            target_bytes=read_int(
                values,
                Env.BKG_OWNER_ARRAY_MAX_BYTES,
                cls.target_bytes,
                minimum=1,
            ),
            maximum_probe=read_int(
                values,
                Env.BKG_OWNER_ARRAY_ADAPTIVE_MAX_PROBE,
                cls.maximum_probe,
                minimum=1,
            ),
            estimate_headroom_percent=read_int(
                values,
                Env.BKG_OWNER_ARRAY_DB_ESTIMATE_HEADROOM_PERCENT,
                cls.estimate_headroom_percent,
                minimum=1,
                maximum=100,
            ),
            database_fallback_version_limit=read_int(
                values,
                Env.BKG_OWNER_ARRAY_DB_FALLBACK_VERSION_LIMIT,
                cls.database_fallback_version_limit,
            ),
            version_limit=read_optional_int(
                values,
                Env.BKG_OWNER_ARRAY_VERSION_LIMIT,
            ),
            database_version_limit=read_optional_int(
                values,
                Env.BKG_OWNER_ARRAY_DB_VERSION_LIMIT,
            ),
        )


@dataclass(frozen=True)
class PackageRenderOptions:
    """Rendering controls for one package endpoint."""

    since: str
    version_limit: int
    legacy_table: str | None
    previous_publication: Path | None = None


@dataclass(frozen=True)
class DatabaseAggregateOptions:
    """Rendering controls for one database-backed aggregate."""

    repo: str | None
    size_hint_directory: Path | None
    settings: AggregateSettings
    previous_publication: Path | None = None


def render_version_array(
    source: VersionSource,
    package: PackageRecord,
    *,
    version_limit: int,
) -> list[JsonValue]:
    """Return deterministic package versions with latest/newest preservation."""

    if not source.rows:
        return [_fallback_version(package)]

    newest_id, latest_id = _version_marks(source.rows)
    latest_rows: dict[str, VersionRecord] = {}
    for row in source.rows:
        current = latest_rows.get(row.version_id)
        if current is None or row.date > current.date:
            latest_rows[row.version_id] = row

    rendered: list[JsonValue] = [
        _render_version(row, newest_id=newest_id, latest_id=latest_id)
        for row in latest_rows.values()
    ]
    rendered.sort(key=_rendered_version_sort_key)
    if version_limit < 0:
        return rendered

    mandatory = [version for version in rendered if _is_protected_version(version)]
    optional = [] if version_limit == 0 else rendered[-version_limit:]
    selected: dict[str, JsonValue] = {}
    for version in (*mandatory, *optional):
        selected[_rendered_version_identifier(version)] = version
    return sorted(selected.values(), key=_rendered_version_sort_key)


def render_package(
    snapshot: PackageSnapshot,
    *,
    version_limit: int,
) -> dict[str, JsonValue]:
    """Return the generated package object for one database snapshot."""

    ranked = snapshot.package
    package = ranked.record
    package_ref = package.package_ref
    versions = render_version_array(
        snapshot.versions,
        package,
        version_limit=version_limit,
    )
    counts = version_count_fields(versions)
    return {
        "owner_type": package_ref.owner_type,
        "package_type": package_ref.package_type,
        "owner_id": _owner_identifier(package_ref.owner_id),
        "owner": package_ref.owner,
        "repo": package_ref.repo,
        "package": package_ref.package,
        "date": package.date,
        "size": human_size(package.size),
        "versions": counts["versions"],
        "tagged": counts["tagged"],
        "owner_rank": human_metric(ranked.owner_rank),
        "repo_rank": human_metric(ranked.repo_rank),
        "downloads": human_metric(package.downloads),
        "downloads_month": human_metric(package.downloads_month),
        "downloads_week": human_metric(package.downloads_week),
        "downloads_day": human_metric(package.downloads_day),
        "raw_size": package.size,
        "raw_versions": counts["raw_versions"],
        "raw_tagged": counts["raw_tagged"],
        "raw_owner_rank": ranked.owner_rank,
        "raw_repo_rank": ranked.repo_rank,
        "raw_downloads": package.downloads,
        "raw_downloads_month": package.downloads_month,
        "raw_downloads_week": package.downloads_week,
        "raw_downloads_day": package.downloads_day,
        "version": versions,
    }


def render_package_file(
    repository: PackageRepository,
    package: PackageRef,
    destination: Path,
    options: PackageRenderOptions,
    check_stop: StopCheck,
) -> bool:
    """Atomically render one package endpoint from the database."""

    snapshot = repository.package_snapshot(
        package,
        since=options.since,
        legacy_table=options.legacy_table,
    )
    if snapshot is None:
        raise RenderingError(
            f"no package row found for {package.owner}/{package.package}"
        )
    check_stop()
    with publication_baseline(
        options.previous_publication or destination, check_stop
    ) as baseline:
        value = baseline.preserve(
            render_package(snapshot, version_limit=options.version_limit)
        )
    refresh_version_counts(value)
    _write_json_value(destination, value)
    return bool(snapshot.versions.rows)


def render_database_aggregate(
    repository: PackageRepository,
    owner_id: str,
    destination: Path,
    options: DatabaseAggregateOptions,
    check_stop: StopCheck,
) -> int:
    """Render an owner or repository aggregate in one streaming database pass."""

    version_limit = _database_version_limit(
        repository,
        owner_id,
        repo=options.repo,
        size_hint_directory=options.size_hint_directory,
        settings=options.settings,
    )
    count = 0
    first = True
    with (
        publication_baseline(
            options.previous_publication or destination, check_stop
        ) as baseline,
        atomic_text_output(destination) as output,
    ):
        output.write("[")

        def visit(snapshot: PackageSnapshot) -> None:
            nonlocal count, first
            check_stop()
            if not first:
                output.write(",")
            value = _preserve_package_endpoint(
                render_package(snapshot, version_limit=version_limit),
                snapshot.package.record.package_ref,
                options,
                check_stop,
            )
            preserved = baseline.preserve(value)
            refresh_version_counts(preserved)
            _dump_json(preserved, output)
            first = False
            count += 1

        repository.visit_owner_snapshots(
            owner_id,
            repo=options.repo,
            visit=visit,
        )
        output.write("]\n")
    return count


def _preserve_package_endpoint(
    value: JsonValue,
    package: PackageRef,
    options: DatabaseAggregateOptions,
    check_stop: StopCheck,
) -> JsonValue:
    directory = options.size_hint_directory
    if directory is None:
        return value
    if options.repo is None:
        directory = directory / package.repo
    endpoint = directory / f"{package.package}.json"
    if not endpoint.is_file() and not endpoint.with_suffix(".xml").is_file():
        return value
    with publication_baseline(endpoint, check_stop) as baseline:
        return baseline.preserve(value)


def render_file_aggregate(
    source_directory: Path,
    destination: Path,
    *,
    settings: AggregateSettings,
    check_stop: StopCheck,
) -> int:
    """Render an adaptive aggregate from existing package JSON files."""

    paths = _package_json_paths(source_directory)
    explicit_limit = settings.version_limit
    with atomic_path(destination) as staged:
        if explicit_limit is not None:
            return _write_file_aggregate(paths, staged, explicit_limit, check_stop)
        if _source_size(paths) <= settings.target_bytes:
            return _write_file_aggregate(paths, staged, -1, check_stop)
        return _write_adaptive_file_aggregate(paths, staged, settings, check_stop)


def _write_adaptive_file_aggregate(
    paths: Sequence[Path],
    destination: Path,
    settings: AggregateSettings,
    check_stop: StopCheck,
) -> int:
    best, count = _aggregate_attempt(paths, destination.parent, 0, check_stop)
    try:
        if best.stat().st_size > settings.target_bytes:
            best.replace(destination)
            return count

        low = 0
        high = 1
        while high <= settings.maximum_probe:
            candidate, candidate_count = _aggregate_attempt(
                paths,
                destination.parent,
                high,
                check_stop,
            )
            if candidate.stat().st_size <= settings.target_bytes:
                best.unlink()
                best = candidate
                count = candidate_count
                low = high
                high *= 2
                continue
            candidate.unlink()
            break

        if high <= settings.maximum_probe:
            while low + 1 < high:
                middle = (low + high) // 2
                candidate, candidate_count = _aggregate_attempt(
                    paths,
                    destination.parent,
                    middle,
                    check_stop,
                )
                if candidate.stat().st_size <= settings.target_bytes:
                    best.unlink()
                    best = candidate
                    count = candidate_count
                    low = middle
                else:
                    candidate.unlink()
                    high = middle

        best.replace(destination)
        return count
    finally:
        best.unlink(missing_ok=True)


def _aggregate_attempt(
    paths: Sequence[Path],
    directory: Path,
    version_limit: int,
    check_stop: StopCheck,
) -> tuple[Path, int]:
    descriptor, name = tempfile.mkstemp(dir=directory, prefix=".aggregate.")
    os.close(descriptor)
    path = Path(name)
    try:
        count = _write_file_aggregate(paths, path, version_limit, check_stop)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path, count


def _write_file_aggregate(
    paths: Sequence[Path],
    destination: Path,
    version_limit: int,
    check_stop: StopCheck,
) -> int:
    count = 0
    first = True
    with destination.open("w", encoding="utf-8", newline="\n") as output:
        output.write("[")
        for path in paths:
            check_stop()
            package = _load_package_json(path)
            _limit_file_versions(package, version_limit)
            refresh_version_counts(package)
            if not first:
                output.write(",")
            _dump_json(package, output)
            first = False
            count += 1
        output.write("]\n")
    return count


def _database_version_limit(
    repository: PackageRepository,
    owner_id: str,
    *,
    repo: str | None,
    size_hint_directory: Path | None,
    settings: AggregateSettings,
) -> int:
    explicit_limit = settings.version_limit
    if explicit_limit is not None:
        return explicit_limit

    if size_hint_directory is not None:
        paths = _package_json_paths(size_hint_directory)
        if paths and _source_size(paths) <= settings.target_bytes:
            return -1

    database_limit = settings.database_version_limit
    if database_limit is not None:
        return database_limit

    return repository.estimate_owner_version_limit(
        owner_id,
        VersionLimitEstimate(
            repo=repo,
            target_bytes=settings.target_bytes,
            headroom_percent=settings.estimate_headroom_percent,
            fallback_limit=settings.database_fallback_version_limit,
        ),
    )


def _load_package_json(path: Path) -> dict[str, JsonValue]:
    try:
        value: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RenderingError(f"invalid package JSON {path}: {error}") from error
    if not isinstance(value, dict):
        raise RenderingError(f"package JSON must contain an object: {path}")
    mapping = cast(dict[object, object], value)
    if not all(isinstance(key, str) for key in mapping):
        raise RenderingError(f"package JSON must contain an object: {path}")
    return cast(dict[str, JsonValue], mapping)


def _limit_file_versions(package: dict[str, JsonValue], limit: int) -> None:
    versions = package.get("version")
    if limit < 0 or not isinstance(versions, list):
        return
    objects: list[JsonValue] = [value for value in versions if isinstance(value, dict)]
    objects.sort(key=_rendered_version_sort_key)
    mandatory = [value for value in objects if _is_protected_version(value)]
    optional = [] if limit == 0 else objects[-limit:]
    selected: dict[str, JsonValue] = {}
    for version in (*mandatory, *optional):
        selected[_rendered_version_identifier(version)] = version
    package["version"] = sorted(
        selected.values(),
        key=_rendered_version_sort_key,
    )


def _version_marks(rows: Sequence[VersionRecord]) -> tuple[int, int]:
    numeric_rows = [
        (numeric_id, row)
        for row in rows
        if (numeric_id := _numeric_identifier(row.version_id)) is not None
    ]
    newest = max((numeric_id for numeric_id, _ in numeric_rows), default=-1)
    predicates: tuple[Callable[[str], bool], ...] = (
        lambda tags: "latest" in _compact_tags(tags).split(","),
        lambda tags: all(character not in tags for character in "^~-"),
        lambda tags: "^" not in tags and "~" not in tags,
        lambda tags: "^" not in tags,
        lambda tags: tags != "",
    )
    latest = -1
    for predicate in predicates:
        candidates = [
            numeric_id
            for numeric_id, row in numeric_rows
            if row.tags and predicate(row.tags)
        ]
        if candidates:
            latest = max(candidates)
            break
    if latest < 0:
        fallback = max((row.version_id for row in rows), default="")
        fallback_id = _numeric_identifier(fallback)
        latest = -1 if fallback_id is None else fallback_id
    return newest, latest


def _render_version(
    version: VersionRecord,
    *,
    newest_id: int,
    latest_id: int,
) -> dict[str, JsonValue]:
    numeric_id = _numeric_identifier(version.version_id)
    metrics = version.metrics
    return {
        "id": numeric_id if numeric_id is not None else version.version_id,
        "name": version.name,
        "date": version.date,
        "newest": version.version_id == str(newest_id),
        "latest": version.version_id == str(latest_id),
        "size": human_size(metrics.size),
        "downloads": human_metric(metrics.downloads),
        "downloads_month": human_metric(metrics.downloads_month),
        "downloads_week": human_metric(metrics.downloads_week),
        "downloads_day": human_metric(metrics.downloads_day),
        "raw_size": metrics.size,
        "raw_downloads": metrics.downloads,
        "raw_downloads_month": metrics.downloads_month,
        "raw_downloads_week": metrics.downloads_week,
        "raw_downloads_day": metrics.downloads_day,
        "tags": [tag.strip() for tag in version.tags.split(",") if tag.strip()],
    }


def _fallback_version(package: PackageRecord) -> dict[str, JsonValue]:
    return {
        "id": -1,
        "name": "latest",
        "date": package.date,
        "newest": True,
        "latest": True,
        "size": human_size(package.size),
        "downloads": human_metric(package.downloads),
        "downloads_month": human_metric(package.downloads_month),
        "downloads_week": human_metric(package.downloads_week),
        "downloads_day": human_metric(package.downloads_day),
        "raw_size": package.size,
        "raw_downloads": package.downloads,
        "raw_downloads_month": package.downloads_month,
        "raw_downloads_week": package.downloads_week,
        "raw_downloads_day": package.downloads_day,
        "tags": [],
    }


def _rendered_version_sort_key(value: JsonValue) -> tuple[int, int, str]:
    identifier = value.get("id") if isinstance(value, dict) else value
    numeric = _numeric_identifier(str(identifier))
    return (
        0 if numeric is not None else 1,
        numeric or 0,
        str(identifier),
    )


def _rendered_version_identifier(value: JsonValue) -> str:
    if not isinstance(value, dict):
        return str(value)
    return str(value.get("id"))


def _is_protected_version(value: JsonValue) -> bool:
    return isinstance(value, dict) and (
        value.get("latest") is True or value.get("newest") is True
    )


def _numeric_identifier(value: str) -> int | None:
    return int(value) if value and value.isdecimal() else None


def _compact_tags(value: str) -> str:
    return "".join(value.split())


def _owner_identifier(value: str) -> JsonValue:
    return int(value) if value.isdecimal() else value


def _package_json_paths(directory: Path) -> tuple[Path, ...]:
    if not directory.is_dir():
        return ()
    return tuple(
        sorted(
            path for path in directory.rglob("*.json") if not path.name.startswith(".")
        )
    )


def _source_size(paths: Sequence[Path]) -> int:
    return sum(path.stat().st_size for path in paths) + len(paths) + 2


def _write_json_value(destination: Path, value: JsonValue) -> None:
    with atomic_text_output(destination) as output:
        _dump_json(value, output)
        output.write("\n")


def _dump_json(value: JsonValue, output: TextIO) -> None:
    json.dump(
        value,
        output,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )
