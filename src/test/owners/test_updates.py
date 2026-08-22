"""Tests for owner scan verification and repository identity changes."""

from __future__ import annotations

from pathlib import Path

from bkg_py.database.composition import DatabaseRepositories
from bkg_py.database.models import (
    OwnerScanPackage,
    PackageRecord,
    PackageRef,
)
from bkg_py.database.settings import DatabaseSettings
from bkg_py.owners.updates import (
    OwnerScanVerificationRequest,
    OwnerScanVerificationService,
)

from ..github.fake import FakeGitHubClient


def _package(repo: str) -> PackageRef:
    return PackageRef(
        "42",
        "users",
        "container",
        "example",
        repo,
        "demo%2Fworker",
    )


def _write_package(
    repository: DatabaseRepositories,
    package: PackageRef,
    date: str = "2026-06-01",
) -> None:
    repository.packages.write_package(PackageRecord(package, 1, 1, 1, 1, 1, date))


def test_verification_replaces_duplicate_aliases_and_removes_them_after_publish(
    tmp_path: Path,
) -> None:
    """One API check canonicalizes aliases and retires them only after replacement."""

    repository = DatabaseRepositories(DatabaseSettings(tmp_path / "index.db"))
    first_alias = _package("OldRepo")
    second_alias = _package("OtherRepo")
    _write_package(repository, first_alias, "2026-05-31")
    _write_package(repository, second_alias)
    repository.owners.begin_owner_scan("42", "example", "scan-1", 100)
    fallback = OwnerScanPackage(
        "users",
        "container",
        "demo%2Fworker",
        "demo%2Fworker",
    )
    repository.owners.observe_owner_scan("42", "scan-1", (fallback,), 101)
    client = FakeGitHubClient(
        rest_values={
            "users/example/packages/container/demo%2Fworker": {"repository": None}
        }
    )

    result = OwnerScanVerificationService(
        repository.owners, client, lambda: None
    ).verify(
        OwnerScanVerificationRequest(
            "42",
            "example",
            "scan-1",
            "2026-06-10",
            102,
        )
    )

    assert client.rest_requests == ["users/example/packages/container/demo%2Fworker"]
    assert result.checked_count == 1
    assert result.absent_count == 0
    assert result.work == (fallback,)
    assert not result.changes

    incomplete = repository.owners.complete_owner_scan(
        "42",
        "scan-1",
        "2026-06-10",
        103,
    )
    assert incomplete.pending == (fallback,)
    assert incomplete.removed == ()

    repository.owners.begin_owner_scan("42", "example", "scan-2", 104)
    repository.owners.observe_owner_scan("42", "scan-2", (fallback,), 105)
    _write_package(repository, _package("demo%2Fworker"), "2026-06-10")
    complete = repository.owners.complete_owner_scan(
        "42",
        "scan-2",
        "2026-06-10",
        106,
    )
    assert complete.pending == ()
    assert complete.removed == (first_alias, second_alias)


def test_verification_forces_work_after_changing_the_staged_repository(
    tmp_path: Path,
) -> None:
    """A canonical identity change republishes even when its row is current."""

    repository = DatabaseRepositories(DatabaseSettings(tmp_path / "index.db"))
    canonical = _package("CanonicalRepo")
    _write_package(repository, canonical, "2026-06-10")
    repository.owners.begin_owner_scan("42", "example", "scan-1", 100)
    fallback = OwnerScanPackage(
        "users",
        "container",
        "demo%2Fworker",
        "demo%2Fworker",
    )
    repository.owners.observe_owner_scan("42", "scan-1", (fallback,), 101)
    client = FakeGitHubClient(
        rest_values={
            "users/example/packages/container/demo%2Fworker": {
                "repository": {"name": "CanonicalRepo"}
            }
        }
    )

    result = OwnerScanVerificationService(
        repository.owners, client, lambda: None
    ).verify(
        OwnerScanVerificationRequest(
            "42",
            "example",
            "scan-1",
            "2026-06-10",
            102,
        )
    )

    expected = OwnerScanPackage(
        "users", "container", "CanonicalRepo", canonical.package
    )
    assert result.work == (expected,)
    assert result.changes[0].previous_repositories == ("demo%2Fworker",)
    assert result.changes[0].repository == "CanonicalRepo"


def test_verification_leaves_unavailable_missing_packages_unobserved(
    tmp_path: Path,
) -> None:
    """A package API 404 leaves a truly absent known identity removable."""

    repository = DatabaseRepositories(DatabaseSettings(tmp_path / "index.db"))
    missing = _package("OldRepo")
    _write_package(repository, missing)
    repository.owners.begin_owner_scan("42", "example", "scan-1", 100)
    client = FakeGitHubClient(
        rest_values={"users/example/packages/container/demo%2Fworker": None}
    )

    result = OwnerScanVerificationService(
        repository.owners, client, lambda: None
    ).verify(
        OwnerScanVerificationRequest(
            "42",
            "example",
            "scan-1",
            "2026-06-10",
            101,
        )
    )
    completed = repository.owners.complete_owner_scan(
        "42",
        "scan-1",
        "2026-06-10",
        102,
    )

    assert result.checked_count == 1
    assert result.absent_count == 1
    assert not result.work
    assert completed.removed == (missing,)
