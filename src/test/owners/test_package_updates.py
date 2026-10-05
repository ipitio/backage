"""Tests for bounded in-process owner package refreshes."""

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from threading import Barrier

import pytest

import bkg_py.packages.updates
from bkg_py.concurrency import BoundedWorkerRunner, ConcurrencySettings
from bkg_py.database.composition import DatabaseRepositories
from bkg_py.database.models import (
    OwnerScanPackage,
    OwnerScanPage,
    PackageBatch,
    PackageRecord,
    PackageRef,
)
from bkg_py.database.settings import DatabaseSettings
from bkg_py.github import GitHubNotFoundError
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
from bkg_py.owners.updates import OwnerScanOutcome, OwnerScanService
from bkg_py.packages.discovery import PackageDiscoveryError
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
from bkg_py.publication.promotion import PublicationRecoveryError
from bkg_py.runtime import GracefulStop

from ..github.fake import FakeGitHubClient


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
        DatabaseRepositories(DatabaseSettings(tmp_path / "index.db")).packages,
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
            PackageRefreshPolicy(True, True, 0),
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
        DatabaseRepositories(DatabaseSettings(tmp_path / "index.db")).packages,
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
        PackageRefreshPolicy(True, True, 0),
    )

    with pytest.raises(GracefulStop, match="test stop"):
        service.refresh(request)


@pytest.mark.parametrize("concurrent_stop", [False, True])
def test_owner_package_refresh_propagates_failed_restoration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    concurrent_stop: bool,
) -> None:
    """Unrestored files abort the run even when a sibling stops gracefully."""

    count = 2 if concurrent_stop else 1
    barrier = Barrier(count)

    def refresh(
        _service: PackageRefreshService,
        request: PackageRefreshRequest,
    ) -> PackageRefreshResult:
        barrier.wait(timeout=5)
        if request.package_ref.package == "pkg-1":
            raise GracefulStop("concurrent stop")
        raise PublicationRecoveryError("retained outputs")

    monkeypatch.setattr(PackageRefreshService, "refresh", refresh)
    service = OwnerPackageRefreshService(
        DatabaseRepositories(DatabaseSettings(tmp_path / "index.db")).packages,
        FakeGitHubClient(),
        _execution(tmp_path, [], []),
    )
    request = OwnerPackageRefreshRequest(
        "42",
        "example",
        tuple(
            OwnerScanPackage("orgs", "container", "repo", f"pkg-{index}")
            for index in range(count)
        ),
        PackageBatch("2026-06-28"),
        "versions",
        tmp_path / "index",
        PackageRefreshPolicy(True, True, 0),
    )

    with pytest.raises(PublicationRecoveryError, match="retained outputs"):
        service.refresh(request)


def test_owner_page_service_advances_multiple_pages_with_one_client(
    tmp_path: Path,
) -> None:
    """One bounded service pass stages and advances every fetched page."""

    repository = DatabaseRepositories(DatabaseSettings(tmp_path / "index.db"))
    package = PackageRef(
        "42",
        "orgs",
        "container",
        "example",
        "repo",
        "package",
    )
    repository.packages.write_package(
        PackageRecord(package, 1, 1, 1, 1, 1, "2026-06-28")
    )
    repository.packages.mark_package_batch_completed(package, "batch-1", "2026-06-28")
    departed = PackageRef(
        "42",
        "orgs",
        "container",
        "example",
        "old-repo",
        "departed",
    )
    repository.packages.write_package(
        PackageRecord(departed, 1, 1, 1, 1, 1, "2026-06-27")
    )
    marker = "batch-1:42:100"
    repository.owners.begin_owner_scan("42", "example", marker, 100)
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
            second_url: """
                <div id="org-packages"><h3>0 packages</h3>
                  <div class="blankslate"><h3>No results matched your search.</h3></div>
                </div>
            """,
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
        PackageRefreshPolicy(True, True, 0),
    )
    timestamps = iter((101, 102, 103, 104, 105, 106))

    package_refresh = OwnerPackageRefreshService(
        repository.packages,
        client,
        _execution(tmp_path, progress, []),
    )
    pages = OwnerScanPageService(
        repository.owners,
        client,
        package_refresh,
        OwnerScanPageExecution(
            lambda: None,
            progress.append,
            now=lambda: next(timestamps),
        ),
    )
    result = OwnerScanService(
        repository.owners,
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
    cursor = repository.owners.current_owner_scan("42", "batch-1")
    assert cursor is None
    assert progress == [
        "Starting example page 1...",
        "Started example page 1",
        "Starting example page 2...",
        "Started example page 2",
    ]


@pytest.mark.parametrize("start_page", [1, 2])
@pytest.mark.parametrize(
    "html",
    [
        "<div>upstream changed</div>",
        '<div id="org-packages"><h3>2 packages</h3><ul>'
        '<li><a href="/orgs/example/packages/container/package/package">known</a>'
        '<a href="/example/repo">repository</a></li>'
        '<li><a href="/example/repo/packages/12345">legacy</a></li></ul></div>',
    ],
    ids=["unrecognized", "mixed-inventory"],
)
def test_unrecognized_owner_page_keeps_cursor_and_package_inventory(
    tmp_path: Path, start_page: int, html: str
) -> None:
    """Neither a new nor a resumed scan can advance across unknown HTML."""

    repository = DatabaseRepositories(DatabaseSettings(tmp_path / "index.db"))
    package = PackageRef("42", "orgs", "container", "example", "repo", "package")
    repository.packages.write_package(
        PackageRecord(package, 1, 1, 1, 1, 1, "2026-06-28")
    )
    marker = "batch-1:42:100"
    repository.owners.begin_owner_scan("42", "example", marker, 100)
    observed = (OwnerScanPackage("orgs", "container", "repo", "package"),)
    if start_page == 2:
        page = OwnerScanPage("42", marker, 1, 101)
        repository.owners.observe_owner_scan_page(page, observed)
        repository.owners.advance_owner_scan_page(page)
    url = (
        "https://github.com/orgs/example/packages?visibility=public&per_page=100"
        f"&page={start_page}"
    )
    client = FakeGitHubClient(
        text_values={
            url: html,
            "https://github.com/example/repo/packages/12345": "<h1>Unknown format</h1>",
        }
    )
    refresh = OwnerPackageRefreshRequest(
        "42",
        "example",
        (),
        PackageBatch("2026-06-28", "batch-1"),
        "versions",
        tmp_path / "index",
        PackageRefreshPolicy(True, False, 0),
    )
    service = OwnerPackageRefreshService(
        repository.packages, client, _execution(tmp_path, [], [])
    )
    pages = OwnerScanPageService(
        repository.owners,
        client,
        service,
        OwnerScanPageExecution(lambda: None, lambda _message: None, now=lambda: 102),
    )

    with pytest.raises(PackageDiscoveryError, match="unrecognized package listing"):
        OwnerScanService(repository.owners, client, pages, service).scan(
            OwnerScanPagesRequest("orgs", marker, start_page, 0, refresh)
        )

    resumed = DatabaseRepositories(DatabaseSettings(tmp_path / "index.db"))
    cursor = resumed.owners.current_owner_scan("42", "batch-1")
    assert cursor is not None
    assert cursor.next_page == start_page
    assert resumed.owners.observed_owner_scan_packages("42", marker) == (
        observed if start_page == 2 else ()
    )
    assert resumed.packages.package_snapshot(package, since="0000-00-00") is not None
    assert not client.rest_requests


@dataclass(frozen=True)
class _ExclusionStorage:
    root: Path
    repository: DatabaseRepositories
    excluded: PackageRef
    destination: Path


def _exclusion_storage(
    tmp_path: Path,
    stored: str,
    eligible_due: bool,
) -> _ExclusionStorage:
    """Seed the excluded and eligible identities independently of listing state."""

    database_path = tmp_path / "index.db"
    repository = DatabaseRepositories(DatabaseSettings(database_path))
    excluded = PackageRef("42", "orgs", "container", "example", "repo", "excluded")
    eligible = PackageRef("42", "orgs", "container", "example", "repo", "eligible")
    if stored != "unseen":
        date = "2026-06-28" if stored == "completed" else "2026-06-27"
        repository.packages.write_package(PackageRecord(excluded, 1, 1, 1, 1, 1, date))
        if stored == "completed":
            repository.packages.mark_package_batch_completed(excluded, "batch-1", date)
    repository.packages.write_package(
        PackageRecord(eligible, 1, 1, 1, 1, 1, "2026-06-28")
    )
    if not eligible_due:
        repository.packages.mark_package_batch_completed(
            eligible, "batch-1", "2026-06-28"
        )
    destination = tmp_path / "index" / "example" / "repo" / "excluded.json"
    destination.parent.mkdir(parents=True)
    destination.write_text("{}\n", encoding="utf-8")
    destination.with_suffix(".xml").write_text("<xml/>\n", encoding="utf-8")
    (tmp_path / "optout.txt").write_text("example/repo/excluded\n", encoding="utf-8")
    return _ExclusionStorage(tmp_path, repository, excluded, destination)


def _exclusion_listing_client(
    source: str,
    start_page: int,
    eligible_due: bool,
) -> tuple[FakeGitHubClient, list[str]]:
    """Offer package metadata only for actual due work and identity verification."""

    listing_url = (
        "https://github.com/orgs/example/packages?visibility=public&per_page=100"
        f"&page={start_page}"
    )
    names = ("excluded", "eligible") if source == "page" else ("eligible",)
    listing = "".join(
        f'<a href="/orgs/example/packages/container/package/{name}">{name}</a>'
        '<a href="/example/repo">repo</a>'
        for name in names
    )
    eligible_url = "https://github.com/orgs/example/packages/container/package/eligible"
    client = FakeGitHubClient(
        text_values={
            listing_url: listing,
            eligible_url: GitHubNotFoundError("temporary missing metadata"),
        },
        rest_values={
            "orgs/example/packages/container/excluded": {"repository": {"name": "repo"}}
        },
    )
    return client, [listing_url, *([eligible_url] if eligible_due else [])]


def _scan_exclusion(
    storage: _ExclusionStorage,
    source: str,
    eligible_due: bool,
) -> OwnerScanOutcome:
    """Resume the real page, verification, and reconciliation services offline."""

    marker = "batch-1:42:100"
    storage.repository.owners.begin_owner_scan("42", "example", marker, 100)
    start_page = 1 if source == "page" else 2
    if start_page == 2:
        observed = (
            (OwnerScanPackage("orgs", "container", "repo", "excluded"),)
            if source == "resumed"
            else ()
        )
        page = OwnerScanPage("42", marker, 1, 101)
        storage.repository.owners.observe_owner_scan_page(page, observed)
        storage.repository.owners.advance_owner_scan_page(page)
    client, expected_requests = _exclusion_listing_client(
        source, start_page, eligible_due
    )
    refresh_request = OwnerPackageRefreshRequest(
        "42",
        "example",
        (),
        PackageBatch("2026-06-28", "batch-1"),
        "versions",
        storage.root / "index",
        PackageRefreshPolicy(True, True, 0),
    )
    package_refresh = OwnerPackageRefreshService(
        storage.repository.packages, client, _execution(storage.root, [], [])
    )
    pages = OwnerScanPageService(
        storage.repository.owners,
        client,
        package_refresh,
        OwnerScanPageExecution(lambda: None, lambda _message: None, now=lambda: 102),
    )
    result = OwnerScanService(
        storage.repository.owners, client, pages, package_refresh
    ).scan(OwnerScanPagesRequest("orgs", marker, start_page, 0, refresh_request))
    assert client.text_requests == expected_requests
    return result


@pytest.mark.parametrize(
    ("stored", "source", "eligible_due"),
    [
        ("unseen", "page", False),
        ("stale", "page", True),
        ("completed", "page", False),
        ("unseen", "resumed", True),
        ("completed", "resumed", False),
        ("completed", "verification", False),
    ],
)
def test_owner_scan_finishes_exclusions_without_losing_due_work(
    tmp_path: Path,
    stored: str,
    source: str,
    eligible_due: bool,
) -> None:
    """Excluded identities leave staging, even after completion or page resume."""

    storage = _exclusion_storage(tmp_path, stored, eligible_due)
    result = _scan_exclusion(storage, source, eligible_due)

    assert result.reconciliation is not None
    completion = result.reconciliation.completion
    assert completion.pending == (
        (OwnerScanPackage("orgs", "container", "repo", "eligible"),)
        if eligible_due
        else ()
    )
    assert bool(completion.retry_after) == eligible_due
    assert (
        storage.repository.packages.package_snapshot(
            storage.excluded, since="0000-00-00"
        )
        is None
    )
    assert not storage.destination.exists()
    assert not storage.destination.with_suffix(".xml").exists()
    with sqlite3.connect(tmp_path / "index.db") as connection:
        catalog_names = connection.execute(
            "select package from bkg_package_catalog where owner = ?", ("example",)
        ).fetchall()
    assert catalog_names == [("eligible",)]


def test_owner_scan_keeps_failed_exclusion_cleanup_pending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cleanup failure overrides an old completion marker and survives retry."""

    storage = _exclusion_storage(tmp_path, "completed", False)

    def deny_cleanup(_destination: Path) -> None:
        raise PermissionError("cleanup denied")

    with monkeypatch.context() as failing:
        failing.setattr(
            bkg_py.packages.updates, "remove_package_artifacts", deny_cleanup
        )
        result = _scan_exclusion(storage, "page", False)

    assert result.reconciliation is not None
    completion = result.reconciliation.completion
    assert completion.pending == (
        OwnerScanPackage("orgs", "container", "repo", "excluded"),
    )
    assert completion.retry_after > 0
    assert (
        storage.repository.packages.package_snapshot(
            storage.excluded, since="0000-00-00"
        )
        is not None
    )
    assert storage.repository.packages.package_publication_pending(storage.excluded)
    assert storage.destination.exists()

    retried = _scan_exclusion(storage, "page", False)
    assert retried.reconciliation is not None
    assert retried.reconciliation.completion.pending_count == 0
    assert retried.reconciliation.completion.retry_after == 0
    assert not storage.repository.packages.package_publication_pending(storage.excluded)
    assert not storage.destination.exists()
