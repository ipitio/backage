"""Tests for bounded in-process owner package refreshes."""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from bkg_py import ExitStatus
from bkg_py.cli import main
from bkg_py.concurrency import BoundedWorkerRunner, ConcurrencySettings
from bkg_py.database import DatabaseRepository, DatabaseSettings
from bkg_py.database_models import OwnerScanPackage
from bkg_py.owner_package_updates import (
    OwnerPackageRefreshExecution,
    OwnerPackageRefreshRequest,
    OwnerPackageRefreshResult,
    OwnerPackageRefreshService,
    allocate_worker_counts,
)
from bkg_py.package_updates import (
    PackageRefreshError,
    PackageRefreshExecution,
    PackageRefreshPolicy,
    PackageRefreshRequest,
    PackageRefreshResult,
    PackageRefreshService,
)
from bkg_py.publication import PublicationLimits
from bkg_py.runtime import GracefulStop
from bkg_py.version_selection import VersionSelectionSettings
from bkg_py.version_updates import VersionRefreshExecution

from .github_client_fake import FakeGitHubClient


def _execution(
    tmp_path: Path,
    progress: list[str],
    diagnostics: list[str],
) -> OwnerPackageRefreshExecution:
    settings = ConcurrencySettings(max_workers=4)
    return OwnerPackageRefreshExecution(
        PackageRefreshExecution(
            VersionRefreshExecution(
                BoundedWorkerRunner(settings),
                lambda _reference: "",
                diagnostic=diagnostics.append,
            ),
            VersionSelectionSettings(),
            PublicationLimits(),
            tmp_path / "optout.txt",
            lambda: None,
        ),
        settings,
        progress.append,
        diagnostics.append,
    )


@pytest.mark.parametrize(
    ("package_count", "budget", "expected"),
    [(1, 8, (1, 8)), (2, 8, (2, 4)), (10, 8, (8, 1))],
)
def test_worker_counts_share_one_budget_across_nested_work(
    package_count: int,
    budget: int,
    expected: tuple[int, int],
) -> None:
    """Package and version workers stay within one owner worker budget."""

    assert allocate_worker_counts(package_count, budget) == expected


def test_owner_package_refresh_continues_after_expected_package_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One unavailable package remains pending without cancelling its siblings."""

    progress: list[str] = []
    diagnostics: list[str] = []
    version_workers: list[int] = []

    def refresh(
        service: PackageRefreshService,
        request: PackageRefreshRequest,
    ) -> PackageRefreshResult:
        version_workers.append(
            service.execution.version.worker_runner.settings.max_workers
        )
        if request.package_ref.package == "pkg-1":
            raise PackageRefreshError("temporary package failure")
        return PackageRefreshResult("refreshed")

    monkeypatch.setattr(PackageRefreshService, "refresh", refresh)
    packages = tuple(
        OwnerScanPackage("orgs", "container", f"repo-{index}", f"pkg-{index}")
        for index in range(3)
    )
    service = OwnerPackageRefreshService(
        DatabaseRepository(DatabaseSettings(tmp_path / "index.db")),
        FakeGitHubClient(),
        _execution(tmp_path, progress, diagnostics),
    )

    result = service.refresh(
        OwnerPackageRefreshRequest(
            "42",
            "example",
            packages,
            "2026-06-28",
            "versions",
            tmp_path / "index",
            PackageRefreshPolicy(True, True, False, 0),
        )
    )

    assert len(result.items) == 3
    assert result.failure_count == 1
    assert version_workers == [1, 1, 1]
    assert sum(message.startswith("Updating example/pkg-") for message in progress) == 3
    assert any("temporary package failure" in message for message in diagnostics)


def test_owner_package_refresh_propagates_graceful_stop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A package stop halts new owner work and reaches the shell adapter."""

    def refresh(
        _service: PackageRefreshService,
        _request: PackageRefreshRequest,
    ) -> PackageRefreshResult:
        raise GracefulStop("test stop")

    monkeypatch.setattr(PackageRefreshService, "refresh", refresh)
    service = OwnerPackageRefreshService(
        DatabaseRepository(DatabaseSettings(tmp_path / "index.db")),
        FakeGitHubClient(),
        _execution(tmp_path, [], []),
    )
    request = OwnerPackageRefreshRequest(
        "42",
        "example",
        (OwnerScanPackage("orgs", "container", "repo", "package"),),
        "2026-06-28",
        "versions",
        tmp_path / "index",
        PackageRefreshPolicy(True, True, False, 0),
    )

    with pytest.raises(GracefulStop, match="test stop"):
        service.refresh(request)


def test_owner_refresh_packages_cli_reads_refs_from_stdin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shell-facing command converts package refs into one owner batch."""

    captured: list[OwnerPackageRefreshRequest] = []

    def refresh(
        _service: OwnerPackageRefreshService,
        request: OwnerPackageRefreshRequest,
    ) -> OwnerPackageRefreshResult:
        captured.append(request)
        return OwnerPackageRefreshResult(())

    monkeypatch.setattr(OwnerPackageRefreshService, "refresh", refresh)
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO("container/repo-one/pkg-one\nnpm/repo-two/pkg-two\n"),
    )
    monkeypatch.setenv("BKG_ROOT", str(tmp_path))
    monkeypatch.setenv("BKG_INDEX_DB", str(tmp_path / "index.db"))
    monkeypatch.setenv("BKG_INDEX_DIR", str(tmp_path / "index"))
    monkeypatch.setenv("BKG_ENV", str(tmp_path / "state.env"))

    status = main(
        [
            "owner",
            "refresh-packages",
            "42",
            "orgs",
            "example",
            "2026-06-28",
            "false",
        ]
    )

    assert status == ExitStatus.SUCCESS
    assert captured[0].packages == (
        OwnerScanPackage("orgs", "container", "repo-one", "pkg-one"),
        OwnerScanPackage("orgs", "npm", "repo-two", "pkg-two"),
    )
