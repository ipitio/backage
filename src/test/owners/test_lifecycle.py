"""Tests for the Python-owned inner owner update lifecycle."""

import sqlite3
from pathlib import Path
from typing import cast

import pytest

from bkg_py.application import ApplicationContext
from bkg_py.concurrency import ConcurrencySettings
from bkg_py.database.composition import DatabaseRepositories
from bkg_py.database.models import (
    OwnerScanPackage,
    OwnerScanPage,
    OwnerScanResult,
    OwnerScanStart,
    PackageBatch,
    PackageCatalogPath,
    PackageRecord,
    PackageRef,
)
from bkg_py.database.settings import DatabaseSettings
from bkg_py.discovery import OwnerIdentityResolver
from bkg_py.github import GitHubClient
from bkg_py.owners.lifecycle import (
    OwnerLifecycleExecution,
    OwnerLifecycleRequest,
    OwnerLifecycleResult,
    OwnerLifecycleService,
    OwnerLifecycleServices,
)
from bkg_py.owners.operations import (
    OwnerOperationExecution,
    OwnerUpdateRequest,
)
from bkg_py.owners.package_updates import (
    OwnerPackageRefreshRequest,
    OwnerPackageRefreshResult,
    OwnerPackageRefreshService,
)
from bkg_py.owners.publication import (
    OwnerPublicationRequest,
    OwnerPublicationResult,
)
from bkg_py.owners.scan_pages import OwnerScanPagesRequest, OwnerScanPagesResult
from bkg_py.owners.updates import (
    OwnerScanOutcome,
    OwnerScanReconciliation,
    OwnerScanVerificationResult,
    OwnerUpdateError,
)
from bkg_py.packages.updates import PackageRefreshPolicy
from bkg_py.state import StateStore

from ..github.fake import FakeGitHubClient, UnexpectedPackageRegistryClient

_TODAY = "2026-06-29"
_RUN_TODAY = "2026-06-30"


def _package(name: str, date: str = _TODAY) -> tuple[PackageRef, PackageRecord]:
    reference = PackageRef(
        "42",
        "orgs",
        "container",
        "Example",
        f"repo-{name}",
        name,
    )
    return reference, PackageRecord(reference, 1, 1, 1, 1, 1, date)


def _request(tmp_path: Path) -> OwnerLifecycleRequest:
    return OwnerLifecycleRequest(
        "orgs",
        0,
        OwnerPackageRefreshRequest(
            "42",
            "Example",
            (),
            PackageBatch(_TODAY, "batch-1"),
            "versions",
            tmp_path / "index",
            PackageRefreshPolicy(True, True, False, 0),
        ),
    )


def _completed_scan(
    *,
    first_page_empty: bool = False,
    removed: tuple[PackageRef, ...] = (),
    catalog_removed: tuple[PackageCatalogPath, ...] = (),
) -> OwnerScanOutcome:
    return OwnerScanOutcome(
        OwnerScanPagesResult(
            2,
            1,
            completed=True,
            first_page_empty=first_page_empty,
        ),
        OwnerScanReconciliation(
            OwnerScanVerificationResult(0, 0, (), (), ()),
            OwnerScanResult(removed, (), 0, catalog_removed),
        ),
    )


class _PackageRefresher:  # pylint: disable=too-few-public-methods
    def __init__(
        self,
        repository: DatabaseRepositories,
        *,
        apply_updates: bool = True,
    ) -> None:
        self.repository = repository
        self.apply_updates = apply_updates
        self.requests: list[OwnerPackageRefreshRequest] = []

    def refresh(
        self,
        request: OwnerPackageRefreshRequest,
    ) -> OwnerPackageRefreshResult:
        """Record work and optionally make its database rows current."""

        self.requests.append(request)
        if not self.apply_updates:
            return OwnerPackageRefreshResult(())
        for package in request.packages:
            reference = PackageRef(
                request.owner_id,
                package.owner_type,
                package.package_type,
                request.owner,
                package.repo,
                package.package,
            )
            self.repository.packages.write_package(
                PackageRecord(reference, 1, 1, 1, 1, 1, request.since)
            )
            self.repository.packages.clear_package_publication(reference)
            self.repository.packages.mark_package_batch_completed(
                reference,
                request.batch_marker,
                request.since,
            )
        return OwnerPackageRefreshResult(())


class _Scanner:  # pylint: disable=too-few-public-methods
    def __init__(self, result: OwnerScanOutcome | None = None) -> None:
        self.result = result
        self.requests: list[OwnerScanPagesRequest] = []

    def scan(self, request: OwnerScanPagesRequest) -> OwnerScanOutcome:
        """Record and return the configured scan outcome."""

        self.requests.append(request)
        if self.result is None:
            raise AssertionError("owner scan was not expected")
        return self.result


class _Publisher:  # pylint: disable=too-few-public-methods
    def __init__(self, package_count: int) -> None:
        self.result = OwnerPublicationResult(package_count, ())
        self.requests: list[OwnerPublicationRequest] = []

    def publish(self, request: OwnerPublicationRequest) -> OwnerPublicationResult:
        """Record and return the configured publication outcome."""

        self.requests.append(request)
        return self.result


def _service(
    repository: DatabaseRepositories,
    refresher: _PackageRefresher,
    scanner: _Scanner,
    publisher: _Publisher,
    execution: OwnerLifecycleExecution,
) -> OwnerLifecycleService:
    return OwnerLifecycleService(
        repository.owners,
        OwnerLifecycleServices(refresher, scanner, publisher),
        execution,
    )


def _execution(state: StateStore) -> OwnerLifecycleExecution:
    return OwnerLifecycleExecution(state, lambda _message: None, now=lambda: 100)


def test_direct_partial_refresh_publishes_without_starting_a_scan(
    tmp_path: Path,
) -> None:
    """Resolved known work avoids a redundant complete owner listing."""

    repository = DatabaseRepositories(DatabaseSettings(tmp_path / "index.db"))
    _, current = _package("current")
    stale_ref, stale = _package("stale", "2026-06-28")
    repository.packages.write_package(current)
    repository.packages.write_package(stale)
    repository.packages.mark_package_batch_completed(
        current.package_ref,
        "batch-1",
        _TODAY,
    )
    refresher = _PackageRefresher(repository)
    scanner = _Scanner()
    publisher = _Publisher(2)

    result = _service(
        repository,
        refresher,
        scanner,
        publisher,
        _execution(StateStore(tmp_path / "state.env")),
    ).update(_request(tmp_path))

    assert result.outcome == "updated"
    assert result.scan is None
    assert refresher.requests[0].packages == (
        OwnerScanPackage(
            stale_ref.owner_type,
            stale_ref.package_type,
            stale_ref.repo,
            stale_ref.package,
        ),
    )
    assert not scanner.requests
    assert len(publisher.requests) == 1


def test_publication_only_work_is_replayed_without_owner_discovery(
    tmp_path: Path,
) -> None:
    """Current package data can recover interrupted file publication directly."""

    repository = DatabaseRepositories(DatabaseSettings(tmp_path / "index.db"))
    pending_ref, pending = _package("pending")
    repository.packages.write_package_pending_publication(pending)
    refresher = _PackageRefresher(repository)
    scanner = _Scanner()

    result = _service(
        repository,
        refresher,
        scanner,
        _Publisher(1),
        _execution(StateStore(tmp_path / "state.env")),
    ).update(_request(tmp_path))

    assert result.outcome == "updated"
    assert refresher.requests[0].packages == (
        OwnerScanPackage(
            pending_ref.owner_type,
            pending_ref.package_type,
            pending_ref.repo,
            pending_ref.package,
        ),
    )
    assert not repository.packages.package_publication_pending(pending_ref)
    assert not scanner.requests


def test_unresolved_direct_refresh_falls_back_to_complete_owner_scan(
    tmp_path: Path,
) -> None:
    """Incomplete known work cannot bypass deletion reconciliation."""

    repository = DatabaseRepositories(DatabaseSettings(tmp_path / "index.db"))
    _, current = _package("current")
    repository.packages.write_package(current)
    stale_ref, stale = _package("stale", "2026-06-28")
    repository.packages.write_package(stale)
    repo_directory = tmp_path / "index" / "Example" / stale_ref.repo
    repo_directory.mkdir(parents=True)
    for name in ("stale.json", "stale.json.tmp", "stale.xml", "stale.xml.abs"):
        (repo_directory / name).write_text("stale", encoding="utf-8")
    unrelated = repo_directory / "other.json"
    unrelated.write_text("current", encoding="utf-8")
    tree_only = PackageCatalogPath("Example", "tree-only", "departed")
    tree_only_directory = tmp_path / "index" / tree_only.owner / tree_only.repo
    tree_only_directory.mkdir(parents=True)
    (tree_only_directory / "departed.json").write_text("stale", encoding="utf-8")
    (tree_only_directory / "departed.xml").write_text("stale", encoding="utf-8")
    scanner = _Scanner(
        _completed_scan(removed=(stale_ref,), catalog_removed=(tree_only,))
    )

    result = _service(
        repository,
        _PackageRefresher(repository, apply_updates=False),
        scanner,
        _Publisher(2),
        _execution(StateStore(tmp_path / "state.env")),
    ).update(_request(tmp_path))

    assert result.outcome == "updated"
    assert result.scan is not None
    assert scanner.requests[0].start_page == 1
    assert unrelated.read_text(encoding="utf-8") == "current"
    assert not tuple(repo_directory.glob("stale.*"))
    assert not tree_only_directory.exists()


def test_paused_scan_resumes_and_clears_legacy_state_after_completion(
    tmp_path: Path,
) -> None:
    """A bounded pass preserves its cursor and later completes in place."""

    repository = DatabaseRepositories(DatabaseSettings(tmp_path / "index.db"))
    current_ref, current = _package("current")
    repository.packages.write_package(current)
    cursor = repository.owners.begin_or_resume_owner_scan(
        OwnerScanStart(current_ref.owner_id, current_ref.owner, "batch-1", 90)
    )
    repository.owners.advance_owner_scan_page(
        OwnerScanPage(current_ref.owner_id, cursor.marker, 1, 91)
    )
    state = StateStore(tmp_path / "state.env")
    state.set(f"BKG_OWNER_SCAN_{current_ref.owner_id}", cursor.marker)
    state.set(f"BKG_PAGE_{current_ref.owner_id}", 2)
    refresher = _PackageRefresher(repository)
    paused = _Scanner(OwnerScanOutcome(OwnerScanPagesResult(3, 1)))

    first = _service(
        repository,
        refresher,
        paused,
        _Publisher(1),
        _execution(state),
    ).update(_request(tmp_path))

    assert first.outcome == "paused"
    assert paused.requests[0].start_page == 2
    assert state.get(f"BKG_OWNER_SCAN_{current_ref.owner_id}") == cursor.marker

    completed = _Scanner(_completed_scan())
    second = _service(
        repository,
        refresher,
        completed,
        _Publisher(1),
        _execution(state),
    ).update(_request(tmp_path))

    assert second.outcome == "updated"
    assert completed.requests[0].start_page == 2
    assert state.get(f"BKG_OWNER_SCAN_{current_ref.owner_id}") is None
    assert state.get(f"BKG_PAGE_{current_ref.owner_id}") is None


def test_discovered_empty_owner_is_remembered_after_complete_scan(
    tmp_path: Path,
) -> None:
    """Connection discovery does not repeatedly queue a verified empty owner."""

    database_path = tmp_path / "index.db"
    repository = DatabaseRepositories(DatabaseSettings(database_path))
    state = StateStore(tmp_path / "state.env")
    state.add_to_set("BKG_DISCOVERED_CONNECTION_OWNERS", "42/Example")
    state.set("BKG_OWNER_SCAN_42", "stale-marker")
    state.set("BKG_PAGE_42", 7)
    progress: list[str] = []
    scanner = _Scanner(_completed_scan(first_page_empty=True))

    result = _service(
        repository,
        _PackageRefresher(repository),
        scanner,
        _Publisher(0),
        OwnerLifecycleExecution(state, progress.append, now=lambda: 100),
    ).update(_request(tmp_path))

    with sqlite3.connect(database_path) as connection:
        owner_row = connection.execute(
            "select owner_id, owner, date from owners"
        ).fetchone()
    assert result.outcome == "updated"
    assert result.empty_owner_recorded
    assert owner_row == ("42", "Example", _TODAY)
    assert scanner.requests[0].start_page == 1
    assert state.get("BKG_OWNER_SCAN_42") is None
    assert state.get("BKG_PAGE_42") is None
    assert any("Discarding stale owner scan marker" in line for line in progress)


def test_owner_update_operation_persists_backoff_and_reports_a_deferred_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retryable inner failures remain successful, durable worker outcomes."""

    requests: list[OwnerLifecycleRequest] = []

    def fail_update(
        service: OwnerLifecycleService,
        _request_value: OwnerLifecycleRequest,
    ) -> OwnerLifecycleResult:
        requests.append(_request_value)
        package_refresh = cast(
            OwnerPackageRefreshService,
            service.services.package_refresh,
        )
        assert package_refresh.execution.package.version.today() == _RUN_TODAY
        raise OwnerUpdateError("temporary owner failure")

    def unexpected_owner_type_lookup(
        _resolver: OwnerIdentityResolver,
        _owner: str,
    ) -> str:
        raise AssertionError("known owner type should not use GraphQL")

    monkeypatch.setattr(OwnerLifecycleService, "update", fail_update)
    monkeypatch.setattr(
        OwnerIdentityResolver,
        "owner_type",
        unexpected_owner_type_lookup,
    )
    database_path = tmp_path / "index.db"
    repository = DatabaseRepositories(DatabaseSettings(database_path))
    _package_ref, package_record = _package("known")
    repository.packages.write_package(package_record)
    monkeypatch.setenv("BKG_ROOT", str(tmp_path))
    monkeypatch.setenv("BKG_INDEX_DB", str(database_path))
    monkeypatch.setenv("BKG_INDEX_DIR", str(tmp_path / "index"))
    monkeypatch.setenv("BKG_ENV", str(tmp_path / "state.env"))

    progress: list[str] = []
    application = ApplicationContext.from_env()
    result = application.owner_update_operation(
        cast(GitHubClient, FakeGitHubClient()),
        UnexpectedPackageRegistryClient(),
        OwnerOperationExecution(
            ConcurrencySettings(max_workers=1),
            progress.append,
            progress.append,
        ),
    ).update(
        OwnerUpdateRequest(
            owner_id="42",
            owner="Example",
            since=_TODAY,
            batch_marker="batch-1",
            today=_RUN_TODAY,
        )
    )

    deferred = repository.owner_queue.deferred_owners(0)
    assert result.outcome == "deferred"
    assert result.error == "temporary owner failure"
    assert requests[0].owner_type == "orgs"
    assert deferred == (("Example", result.retry_after),)
    assert any("Deferred Example after failed work" in message for message in progress)
