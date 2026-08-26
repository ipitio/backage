"""Tests for package rendering and bounded owner aggregates."""

import json
import sqlite3
import time
import tracemalloc
from pathlib import Path
from typing import cast

import pytest

from bkg_py.database.composition import DatabaseRepositories
from bkg_py.database.models import (
    PackageRecord,
    PackageRef,
    VersionMetrics,
    VersionRecord,
    VersionStage,
)
from bkg_py.database.package.repository import PackageRepository
from bkg_py.database.settings import DatabaseSettings
from bkg_py.publication.rendering import (
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


def _legacy_table(package: PackageRef) -> str:
    return (
        f"versions_{package.owner_type}_{package.package_type}_{package.owner}_"
        f"{package.repo}_{package.package}"
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
    repository: PackageRepository,
    package: PackageRef,
    versions: tuple[VersionRecord, ...],
    *,
    downloads: int = 1000,
) -> None:
    repository.write_package(_package_record(package, downloads=downloads))
    repository.flush_version_stage(
        VersionStage(
            package_ref=package,
            legacy_table=_legacy_table(package),
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

        repository = DatabaseRepositories(
            DatabaseSettings(tmp_path / "index.db")
        ).packages
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
            legacy_table=_legacy_table(package),
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
    ) -> None:
        """Database aggregates ignore stale files and support repository views."""

        repository = DatabaseRepositories(
            DatabaseSettings(tmp_path / "index.db")
        ).packages
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
        owner_output = tmp_path / "owner.json"
        repo_output = tmp_path / "repo.json"
        options = DatabaseAggregateOptions(
            repo=None,
            size_hint_directory=hints,
            settings=AggregateSettings(version_limit=-1),
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
                settings=AggregateSettings(version_limit=-1),
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
    ) -> None:
        """Legacy-backed packages retain the configured conservative slice."""

        repository = DatabaseRepositories(
            DatabaseSettings(tmp_path / "index.db")
        ).packages
        package = _package(repo="LegacyRepo")
        repository.write_package(_package_record(package))
        legacy_table = _legacy_table(package)
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
        render_file_aggregate(
            source.parent,
            two,
            settings=AggregateSettings(version_limit=2),
            check_stop=lambda: None,
        )
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
    ) -> None:
        """A graceful stop cannot replace the previous complete aggregate."""

        repository = DatabaseRepositories(
            DatabaseSettings(tmp_path / "index.db")
        ).packages
        for number in range(1, 4):
            _write_package(repository, _package(number), (_version(number),))
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
                    settings=AggregateSettings(version_limit=-1),
                ),
                stop,
            )

        assert destination.read_text(encoding="utf-8") == '{"old":true}\n'
        assert not list(tmp_path.glob(".owner.json.*"))

    def test_large_owner_stays_within_time_and_memory_budget(
        self,
        tmp_path: Path,
    ) -> None:
        """A representative large owner stays within its regression budget."""

        repository = DatabaseRepositories(
            DatabaseSettings(tmp_path / "index.db")
        ).packages
        repository.ensure_schema()
        packages = [
            _package(number, repo=f"Repo-{number % 10}") for number in range(150)
        ]
        for number, package in enumerate(packages):
            repository.write_package(
                PackageRecord(
                    package,
                    10_000 - number,
                    300,
                    200,
                    20,
                    400,
                    _TODAY,
                )
            )
        for package in packages:
            repository.flush_version_stage(
                VersionStage(
                    package,
                    _legacy_table(package),
                    False,
                    tuple(
                        _version(
                            version,
                            tags="latest" if version == 1 else "",
                        )
                        for version in range(1, 21)
                    ),
                )
            )
        destination = tmp_path / "large-owner.json"
        started = time.monotonic()

        count = render_database_aggregate(
            repository,
            "69664378",
            destination,
            DatabaseAggregateOptions(
                repo=None,
                size_hint_directory=None,
                settings=AggregateSettings(
                    target_bytes=100_000_000,
                    version_limit=-1,
                ),
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
                settings=AggregateSettings(
                    target_bytes=100_000_000,
                    version_limit=-1,
                ),
            ),
            lambda: None,
        )
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        assert peak < 64 * 1024 * 1024
