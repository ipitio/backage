"""Tests for owner scan verification and repository identity changes."""

from __future__ import annotations

from pathlib import Path

from bkg_py.database import DatabaseRepository, DatabaseSettings
from bkg_py.database_models import (
    OwnerScanPackage,
    PackageRecord,
    PackageRef,
)
from bkg_py.owner_updates import (
    OwnerScanVerificationRequest,
    OwnerScanVerificationService,
)

from .github_client_fake import FakeGitHubClient


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
    repository: DatabaseRepository,
    package: PackageRef,
    date: str = "2026-06-01",
) -> None:
    repository.write_package(PackageRecord(package, 1, 1, 1, 1, 1, date))


def test_verification_replaces_duplicate_aliases_and_removes_them_after_publish(
    tmp_path: Path,
) -> None:
    """One API check canonicalizes aliases and retires them only after replacement."""

    repository = DatabaseRepository(DatabaseSettings(tmp_path / "index.db"))
    first_alias = _package("OldRepo")
    second_alias = _package("OtherRepo")
    _write_package(repository, first_alias, "2026-05-31")
    _write_package(repository, second_alias)
    repository.begin_owner_scan("42", "example", "scan-1", 100)
    fallback = OwnerScanPackage(
        "users",
        "container",
        "demo%2Fworker",
        "demo%2Fworker",
    )
    repository.observe_owner_scan("42", "scan-1", (fallback,), 101)
    client = FakeGitHubClient(
        rest_values={
            "users/example/packages/container/demo%2Fworker": {"repository": None}
        }
    )

    result = OwnerScanVerificationService(repository, client, lambda: None).verify(
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
    assert result.changes[0].previous_repositories == ("OldRepo", "OtherRepo")

    incomplete = repository.complete_owner_scan(
        "42",
        "scan-1",
        "2026-06-10",
        103,
    )
    assert incomplete.pending == (fallback,)
    assert incomplete.removed == ()

    repository.begin_owner_scan("42", "example", "scan-2", 104)
    repository.observe_owner_scan("42", "scan-2", (fallback,), 105)
    _write_package(repository, _package("demo%2Fworker"), "2026-06-10")
    complete = repository.complete_owner_scan(
        "42",
        "scan-2",
        "2026-06-10",
        106,
    )
    assert complete.pending == ()
    assert complete.removed == (first_alias, second_alias)


def test_verification_leaves_unavailable_missing_packages_unobserved(
    tmp_path: Path,
) -> None:
    """A package API 404 leaves a truly absent known identity removable."""

    repository = DatabaseRepository(DatabaseSettings(tmp_path / "index.db"))
    missing = _package("OldRepo")
    _write_package(repository, missing)
    repository.begin_owner_scan("42", "example", "scan-1", 100)
    client = FakeGitHubClient(
        rest_values={"users/example/packages/container/demo%2Fworker": None}
    )

    result = OwnerScanVerificationService(repository, client, lambda: None).verify(
        OwnerScanVerificationRequest(
            "42",
            "example",
            "scan-1",
            "2026-06-10",
            101,
        )
    )
    completed = repository.complete_owner_scan(
        "42",
        "scan-1",
        "2026-06-10",
        102,
    )

    assert result.checked_count == 1
    assert result.absent_count == 1
    assert not result.work
    assert completed.removed == (missing,)
