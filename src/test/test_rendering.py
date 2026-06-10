"""Tests for package rendering and bounded owner aggregates."""

from __future__ import annotations

import json
import sqlite3
import time
import tracemalloc
from pathlib import Path
from typing import cast

import pytest

from bkg_py.database import (
    DatabaseRepository,
    DatabaseSettings,
    PackageRecord,
    PackageRef,
    VersionMetrics,
    VersionRecord,
    VersionStage,
)
from bkg_py.rendering import (
    AggregateSettings,
    DatabaseAggregateOptions,
    render_database_aggregate,
    render_file_aggregate,
    render_package,
)
from bkg_py.runtime import GracefulStop

_TODAY = "2026-06-10"


def _package(number: int = 1, *, repo: str = "Repo") -> PackageRef:
    return PackageRef(
        owner_id="69664378",
        owner_type="orgs",
        package_type="container",
        owner="Lazztech",
        repo=repo,
        package=f"package-{number}",
    )


def _package_record(
    package: PackageRef,
    *,
    downloads: int = 1000,
) -> PackageRecord:
    return PackageRecord(
        package_ref=package,
        downloads=downloads,
        downloads_month=300,
        downloads_week=200,
        downloads_day=20,
        size=400,
        date=_TODAY,
    )


def _version(version_id: int, *, tags: str = "") -> VersionRecord:
    return VersionRecord(
        version_id=str(version_id),
        name=f"sha256:{version_id}",
        metrics=VersionMetrics(
            size=version_id * 1000,
            downloads=version_id * 100,
            downloads_month=version_id * 10,
            downloads_week=version_id * 5,
            downloads_day=version_id,
        ),
        date=_TODAY,
        tags=tags,
    )


def _write_package(
    repository: DatabaseRepository,
    package: PackageRef,
    versions: tuple[VersionRecord, ...],
    *,
    downloads: int = 1000,
) -> None:
    repository.write_package(_package_record(package, downloads=downloads))
    repository.flush_version_stage(
        VersionStage(
            package_ref=package,
            legacy_table=repository.legacy_version_table(package),
            write_legacy=False,
            rows=versions,
        )
    )


def _json_array(path: Path) -> list[dict[str, object]]:
    value: object = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, list)
    return cast(list[dict[str, object]], value)


class TestRendering:
    """Exercise deterministic package and aggregate output behavior."""

    def test_package_rendering_preserves_marks_limits_and_formats(
        self,
        tmp_path: Path,
    ) -> None:
        """Package JSON retains existing marks, limits, and humanized fields."""

        repository = DatabaseRepository(DatabaseSettings(tmp_path / "index.db"))
        package = _package()
        _write_package(
            repository,
            package,
            (
                _version(1, tags="latest"),
                _version(2, tags="stable"),
                _version(3),
                _version(4),
                _version(5),
            ),
        )
        snapshot = repository.package_snapshot(
            package,
            since=_TODAY,
            legacy_table=repository.legacy_version_table(package),
        )

        assert snapshot is not None
        rendered = render_package(snapshot, version_limit=2)
        versions = cast(list[dict[str, object]], rendered["version"])

        assert [version["id"] for version in versions] == [1, 4, 5]
        assert versions[0]["latest"] is True
        assert versions[-1]["newest"] is True
        assert rendered["size"] == "400"
        assert rendered["downloads"] == "1k"
        assert rendered["raw_versions"] == 5
        assert rendered["raw_tagged"] == 2

    def test_database_aggregate_ignores_files_and_filters_repository(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Database aggregates ignore stale files and support repository views."""

        repository = DatabaseRepository(DatabaseSettings(tmp_path / "index.db"))
        first = _package(repo="RepoOne")
        second = _package(2, repo="RepoTwo")
        _write_package(repository, first, (_version(1, tags="latest"), _version(2)))
        _write_package(
            repository,
            second,
            (_version(3, tags="latest"),),
            downloads=2000,
        )
        hints = tmp_path / "index" / "Lazztech"
        hints.mkdir(parents=True)
        (hints / "stale.json").write_text(
            '{"package":"stale"}',
            encoding="utf-8",
        )
        monkeypatch.setenv("BKG_OWNER_ARRAY_VERSION_LIMIT", "-1")

        owner_output = tmp_path / "owner.json"
        repo_output = tmp_path / "repo.json"
        options = DatabaseAggregateOptions(
            repo=None,
            size_hint_directory=hints,
            settings=AggregateSettings(),
        )
        assert (
            render_database_aggregate(
                repository,
                first.owner_id,
                owner_output,
                options,
                lambda: None,
            )
            == 2
        )
        render_database_aggregate(
            repository,
            first.owner_id,
            repo_output,
            DatabaseAggregateOptions(
                repo="RepoOne",
                size_hint_directory=hints,
                settings=AggregateSettings(),
            ),
            lambda: None,
        )

        assert [row["package"] for row in _json_array(owner_output)] == [
            "package-1",
            "package-2",
        ]
        assert [row["repo"] for row in _json_array(repo_output)] == ["RepoOne"]

    def test_legacy_aggregate_uses_conservative_fallback_limit(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Legacy-backed packages retain the configured conservative slice."""

        repository = DatabaseRepository(DatabaseSettings(tmp_path / "index.db"))
        package = _package(repo="LegacyRepo")
        repository.write_package(_package_record(package))
        legacy_table = repository.legacy_version_table(package)
        quoted = legacy_table.replace('"', '""')
        with sqlite3.connect(repository.settings.path) as connection:
            connection.execute(
                f"""
                create table "{quoted}" (
                    id text, name text, size integer, downloads integer,
                    downloads_month integer, downloads_week integer,
                    downloads_day integer, date text, tags text
                )
                """
            )
            connection.executemany(
                f'insert into "{quoted}" values (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                [
                    (
                        str(number),
                        f"sha256:{number}",
                        number,
                        number,
                        number,
                        number,
                        number,
                        _TODAY,
                        "latest" if number == 1 else "",
                    )
                    for number in range(1, 6)
                ],
            )
        monkeypatch.delenv("BKG_OWNER_ARRAY_VERSION_LIMIT", raising=False)
        monkeypatch.delenv("BKG_OWNER_ARRAY_DB_VERSION_LIMIT", raising=False)
        output = tmp_path / "legacy.json"

        render_database_aggregate(
            repository,
            package.owner_id,
            output,
            DatabaseAggregateOptions(
                repo=None,
                size_hint_directory=None,
                settings=AggregateSettings(target_bytes=100_000),
            ),
            lambda: None,
        )

        versions = cast(list[dict[str, object]], _json_array(output)[0]["version"])
        assert [version["id"] for version in versions] == [1, 4, 5]

    def test_file_aggregate_adapts_to_exact_byte_budget(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """File aggregates choose the largest exact slice within their budget."""

        source = tmp_path / "owner" / "repo"
        source.mkdir(parents=True)
        package = {
            "package": "demo",
            "version": [
                {
                    "id": number,
                    "latest": number == 1,
                    "newest": number == 5,
                    "notes": "x" * 2000,
                }
                for number in range(1, 6)
            ],
        }
        (source / "demo.json").write_text(json.dumps(package), encoding="utf-8")
        two = tmp_path / "two.json"
        monkeypatch.setenv("BKG_OWNER_ARRAY_VERSION_LIMIT", "2")
        render_file_aggregate(
            source.parent,
            two,
            settings=AggregateSettings(),
            check_stop=lambda: None,
        )
        monkeypatch.delenv("BKG_OWNER_ARRAY_VERSION_LIMIT")
        adaptive = tmp_path / "adaptive.json"
        target = two.stat().st_size

        render_file_aggregate(
            source.parent,
            adaptive,
            settings=AggregateSettings(target_bytes=target),
            check_stop=lambda: None,
        )

        versions = cast(list[dict[str, object]], _json_array(adaptive)[0]["version"])
        assert [version["id"] for version in versions] == [1, 4, 5]
        assert adaptive.stat().st_size <= target

    def test_interrupted_database_aggregate_preserves_destination(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A graceful stop cannot replace the previous complete aggregate."""

        repository = DatabaseRepository(DatabaseSettings(tmp_path / "index.db"))
        for number in range(1, 4):
            _write_package(repository, _package(number), (_version(number),))
        monkeypatch.setenv("BKG_OWNER_ARRAY_VERSION_LIMIT", "-1")
        destination = tmp_path / "owner.json"
        destination.write_text('{"old":true}\n', encoding="utf-8")
        checks = 0

        def stop() -> None:
            nonlocal checks
            checks += 1
            if checks >= 2:
                raise GracefulStop("test")

        with pytest.raises(GracefulStop):
            render_database_aggregate(
                repository,
                "69664378",
                destination,
                DatabaseAggregateOptions(
                    repo=None,
                    size_hint_directory=None,
                    settings=AggregateSettings(),
                ),
                stop,
            )

        assert destination.read_text(encoding="utf-8") == '{"old":true}\n'
        assert not list(tmp_path.glob(".owner.json.*"))

    def test_large_owner_stays_within_time_and_memory_budget(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A representative large owner stays within its regression budget."""

        repository = DatabaseRepository(DatabaseSettings(tmp_path / "index.db"))
        repository.ensure_schema()
        packages = [
            _package(number, repo=f"Repo-{number % 10}") for number in range(150)
        ]
        with sqlite3.connect(repository.settings.path) as connection:
            connection.executemany(
                """
                insert into packages values (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                [
                    (
                        package.owner_id,
                        package.owner_type,
                        package.package_type,
                        package.owner,
                        package.repo,
                        package.package,
                        10_000 - number,
                        300,
                        200,
                        20,
                        400,
                        _TODAY,
                    )
                    for number, package in enumerate(packages)
                ],
            )
            connection.executemany(
                """
                insert into versions values (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                [
                    (
                        package.owner_id,
                        package.owner_type,
                        package.package_type,
                        package.owner,
                        package.repo,
                        package.package,
                        str(version),
                        f"sha256:{version}",
                        version * 100,
                        version * 10,
                        version,
                        version,
                        version,
                        _TODAY,
                        "latest" if version == 1 else "",
                    )
                    for package in packages
                    for version in range(1, 21)
                ],
            )
        monkeypatch.setenv("BKG_OWNER_ARRAY_VERSION_LIMIT", "-1")
        destination = tmp_path / "large-owner.json"
        started = time.monotonic()

        count = render_database_aggregate(
            repository,
            "69664378",
            destination,
            DatabaseAggregateOptions(
                repo=None,
                size_hint_directory=None,
                settings=AggregateSettings(target_bytes=100_000_000),
            ),
            lambda: None,
        )
        elapsed = time.monotonic() - started

        assert count == 150
        assert len(_json_array(destination)) == 150
        assert elapsed < 10

        tracemalloc.start()
        render_database_aggregate(
            repository,
            "69664378",
            tmp_path / "large-owner-memory.json",
            DatabaseAggregateOptions(
                repo=None,
                size_hint_directory=None,
                settings=AggregateSettings(target_bytes=100_000_000),
            ),
            lambda: None,
        )
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        assert peak < 64 * 1024 * 1024
