"""Tests for bounded in-process owner package refreshes."""

from __future__ import annotations

from pathlib import Path

import pytest

from bkg_py.concurrency import BoundedWorkerRunner, ConcurrencySettings
from bkg_py.database import (
    DatabaseRepository,
    DatabaseSettings,
    OwnerScanPackage,
    PackageBatch,
    PackageRecord,
    PackageRef,
)
from bkg_py.owners.package_updates import (
    OwnerPackageRefreshExecution,
    OwnerPackageRefreshRequest,
    OwnerPackageRefreshService,
    allocate_worker_counts,
)
from bkg_py.owners.scan_pages import (
    OwnerScanPageExecution,
    OwnerScanPageService,
    OwnerScanPagesRequest,
    OwnerScanPagesResult,
)
from bkg_py.owners.updates import OwnerScanService
from bkg_py.packages.registry.artifacts import ArtifactSizeResolver
from bkg_py.packages.updates import (
    PackageRefreshError,
    PackageRefreshExecution,
    PackageRefreshPolicy,
    PackageRefreshRequest,
    PackageRefreshResult,
    PackageRefreshService,
)
from bkg_py.packages.versions.selection import VersionSelectionSettings
from bkg_py.packages.versions.updates import VersionRefreshExecution
from bkg_py.publication import PublicationLimits
from bkg_py.runtime import GracefulStop

from ..github_client_fake import FakeGitHubClient


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
                ArtifactSizeResolver(),
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
            PackageBatch("2026-06-28"),
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
        PackageBatch("2026-06-28"),
        "versions",
        tmp_path / "index",
        PackageRefreshPolicy(True, True, False, 0),
    )

    with pytest.raises(GracefulStop, match="test stop"):
        service.refresh(request)


def test_owner_page_service_advances_multiple_pages_with_one_client(
    tmp_path: Path,
) -> None:
    """One bounded service pass stages and advances every fetched page."""

    repository = DatabaseRepository(DatabaseSettings(tmp_path / "index.db"))
    package = PackageRef(
        "42",
        "orgs",
        "container",
        "example",
        "repo",
        "package",
    )
    repository.write_package(PackageRecord(package, 1, 1, 1, 1, 1, "2026-06-28"))
    repository.mark_package_batch_completed(package, "batch-1", "2026-06-28")
    departed = PackageRef(
        "42",
        "orgs",
        "container",
        "example",
        "old-repo",
        "departed",
    )
    repository.write_package(PackageRecord(departed, 1, 1, 1, 1, 1, "2026-06-27"))
    marker = "batch-1:42:100"
    repository.begin_owner_scan("42", "example", marker, 100)
    first_url = (
        "https://github.com/orgs/example/packages?visibility=public&per_page=100&page=1"
    )
    second_url = (
        "https://github.com/orgs/example/packages?visibility=public&per_page=100&page=2"
    )
    client = FakeGitHubClient(
        rest_values={"orgs/example/packages/container/departed": None},
        text_values={
            first_url: """
                <a href="/orgs/example/packages/container/package/package">pkg</a>
                <a href="/example/repo">repo</a>
                <a rel="next" href="?page=2">next</a>
            """,
            second_url: "<div></div>",
        },
    )
    progress: list[str] = []
    refresh_request = OwnerPackageRefreshRequest(
        "42",
        "example",
        (),
        PackageBatch("2026-06-28", "batch-1"),
        "versions",
        tmp_path / "index",
        PackageRefreshPolicy(True, True, False, 0),
    )
    timestamps = iter((101, 102, 103, 104, 105, 106))

    package_refresh = OwnerPackageRefreshService(
        repository,
        client,
        _execution(tmp_path, progress, []),
    )
    pages = OwnerScanPageService(
        repository,
        client,
        package_refresh,
        OwnerScanPageExecution(
            lambda: None,
            progress.append,
            now=lambda: next(timestamps),
        ),
    )
    result = OwnerScanService(
        repository,
        client,
        pages,
        package_refresh,
    ).scan(
        OwnerScanPagesRequest(
            "orgs",
            marker,
            1,
            0,
            refresh_request,
        )
    )

    assert result.pages == OwnerScanPagesResult(3, 2, completed=True)
    assert result.reconciliation is not None
    assert result.reconciliation.verification.checked_count == 1
    assert result.reconciliation.completion.pending_count == 0
    assert result.reconciliation.completion.removed == (departed,)
    assert client.text_requests == [first_url, second_url]
    assert client.rest_requests == ["orgs/example/packages/container/departed"]
    cursor = repository.current_owner_scan("42", "batch-1")
    assert cursor is None
    assert progress == [
        "Starting example page 1...",
        "Started example page 1",
        "Starting example page 2...",
        "Started example page 2",
    ]
