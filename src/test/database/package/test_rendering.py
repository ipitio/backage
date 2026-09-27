"""Latest-row identity and rank consistency across database renderers."""

from dataclasses import replace
from pathlib import Path

import pytest

from bkg_py.database.composition import DatabaseRepositories
from bkg_py.database.models import (
    PackageRecord,
    PackageRef,
    PackageSnapshot,
    VersionLimitEstimate,
    VersionStage,
)
from bkg_py.database.package.repository import PackageRepository
from bkg_py.database.settings import DatabaseSettings

from ..repository_support import TODAY, YESTERDAY, legacy_table, package, version


def _write_observation(
    repository: PackageRepository, reference: PackageRef, downloads: int, observed: str
) -> None:
    repository.write_package(
        PackageRecord(reference, downloads, -1, -1, -1, -1, observed)
    )
    repository.flush_version_stage(
        VersionStage(
            reference,
            legacy_table(reference),
            False,
            tuple(
                version(str(index), date=observed, downloads=downloads)
                for index in range(1, 4)
            ),
        )
    )


def _snapshots(
    repository: PackageRepository, owner_id: str, repo: str | None = None
) -> list[PackageSnapshot]:
    rows: list[PackageSnapshot] = []
    assert repository.visit_owner_snapshots(
        owner_id, repo=repo, visit=rows.append
    ) == len(rows)
    return rows


def test_snapshot_ranks_include_packages_refreshed_on_earlier_days(
    tmp_path: Path,
) -> None:
    """A package's rank must match the owner aggregate, not the newest scan day."""

    repository = DatabaseRepositories(DatabaseSettings(tmp_path / "index.db")).packages
    older = package(package_name="older")
    newer = package(package_name="newer")
    _write_observation(repository, older, 11, YESTERDAY)
    _write_observation(repository, newer, 30, TODAY)
    aggregate = {
        row.package.record.package_ref: row
        for row in _snapshots(repository, older.owner_id)
    }
    snapshot = repository.package_snapshot(older, since=YESTERDAY)
    assert snapshot is not None
    assert snapshot.package == aggregate[older].package
    assert (snapshot.package.owner_rank, snapshot.package.repo_rank) == (2, 2)


def _equal_name_references(repository: PackageRepository) -> tuple[PackageRef, ...]:
    first = package(repo="RepoA", package_name="same")
    references = (
        first,
        replace(first, repo="RepoB"),
        replace(first, package_type="npm"),
    )
    for reference, downloads, observed in zip(
        references, (11, 999, 30), ("2026-06-08", TODAY, YESTERDAY), strict=True
    ):
        _write_observation(repository, reference, downloads, observed)
    return references


def test_equal_names_keep_reference_metrics_and_versions_separate(
    tmp_path: Path,
) -> None:
    """Equal names in different types/repositories are distinct stored references."""

    repository = DatabaseRepositories(DatabaseSettings(tmp_path / "index.db")).packages
    references = _equal_name_references(repository)
    rows = _snapshots(repository, references[0].owner_id)
    assert {row.package.record.package_ref for row in rows} == set(references)
    for row in rows:
        reference = row.package.record.package_ref
        snapshot = repository.package_snapshot(reference, since="0000-00-00")
        assert snapshot is not None
        assert snapshot.package == row.package
        assert snapshot.versions.rows == row.versions.rows
        assert len(row.versions.rows) == 3
        assert {item.metrics.downloads for item in row.versions.rows} == {
            row.package.record.downloads
        }
    assert repository.maximum_package_downloads(references[0]) == 11
    assert repository.maximum_package_downloads(references[2]) == 30
    assert len(_snapshots(repository, references[0].owner_id, "RepoA")) == 2


@pytest.mark.parametrize(("repo", "expected"), [(None, 0), ("RepoA", 0), ("RepoB", 3)])
def test_adaptive_estimate_counts_the_same_reference_set(
    tmp_path: Path, repo: str | None, expected: int
) -> None:
    """The byte budget includes each package and its own mandatory versions."""

    repository = DatabaseRepositories(DatabaseSettings(tmp_path / "index.db")).packages
    references = _equal_name_references(repository)
    assert (
        repository.estimate_owner_version_limit(
            references[0].owner_id, VersionLimitEstimate(repo, 3300, 100, 5)
        )
        == expected
    )


def test_ties_zero_and_unknown_downloads_keep_existing_rank_semantics(
    tmp_path: Path,
) -> None:
    """Historical maxima and unknown values cannot contaminate latest ranks."""

    repository = DatabaseRepositories(DatabaseSettings(tmp_path / "index.db")).packages
    references = tuple(package(package_name=f"pkg-{index}") for index in range(4))
    _write_observation(repository, references[0], 1000, "2026-06-08")
    for reference, downloads, observed in zip(
        references, (10, 10, 0, -1), (YESTERDAY, TODAY, YESTERDAY, TODAY), strict=True
    ):
        _write_observation(repository, reference, downloads, observed)
    rows = _snapshots(repository, references[0].owner_id)
    assert [row.package.owner_rank for row in rows] == [1, 1, 3, 4]
    assert [row.package.record.downloads for row in rows] == [10, 10, 0, -1]
    for row in rows:
        snapshot = repository.package_snapshot(
            row.package.record.package_ref, since=YESTERDAY
        )
        assert snapshot is not None
        assert snapshot.package == row.package


def test_owner_login_and_type_are_preserved_in_stored_references(
    tmp_path: Path,
) -> None:
    """Read existing six-field references without an implicit identity migration."""

    repository = DatabaseRepositories(DatabaseSettings(tmp_path / "index.db")).packages
    first = package()
    references = (
        first,
        replace(first, owner="Alias"),
        replace(first, owner_type="users"),
    )
    for reference, downloads, observed in zip(
        references, (11, 12, 13), ("2026-06-08", YESTERDAY, TODAY), strict=True
    ):
        _write_observation(repository, reference, downloads, observed)
    rows = _snapshots(repository, first.owner_id)
    assert {row.package.record.package_ref for row in rows} == set(references)
    for row in rows:
        reference = row.package.record.package_ref
        assert (
            repository.maximum_package_downloads(reference)
            == row.package.record.downloads
        )
        assert {item.metrics.downloads for item in row.versions.rows} == {
            row.package.record.downloads
        }
    assert (
        repository.estimate_owner_version_limit(
            first.owner_id, VersionLimitEstimate(None, 3300, 100, 5)
        )
        == 0
    )
