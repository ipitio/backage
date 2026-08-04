"""Tests for Python-owned application startup preparation."""

from __future__ import annotations

from pathlib import Path

import pytest

from bkg_py import run_startup
from bkg_py.database import (
    DatabaseRepository,
    DatabaseSettings,
    PackageCatalogPath,
    PackageInventory,
    PackageRecord,
    PackageRef,
)
from bkg_py.discovery import OwnerIdentityCache
from bkg_py.run_startup import (
    RunStartupExecution,
    RunStartupRequest,
    RunStartupService,
    RunStartupServices,
)
from bkg_py.snapshots import SnapshotPaths, SnapshotStore
from bkg_py.state import StateStore
from bkg_py.workspace import IndexPackageCatalogTree


def _package(owner: str, date: str) -> PackageRecord:
    package = PackageRef(
        owner,
        "users",
        "container",
        owner,
        f"repo-{owner}",
        f"package-{owner}",
    )
    return PackageRecord(package, 1, 1, 1, 1, 1, date)


def _service(
    database_path: Path,
    state: StateStore,
    cache: OwnerIdentityCache,
    progress: list[str],
) -> RunStartupService:
    repository = DatabaseRepository(DatabaseSettings(database_path))
    return RunStartupService(
        RunStartupServices(
            repository,
            SnapshotStore(
                SnapshotPaths(
                    database_path,
                    snapshot_dir=database_path.parent / "snapshot",
                )
            ),
            state,
            cache,
        ),
        RunStartupExecution(lambda: None, progress.append, now=lambda: 100),
    )


def test_startup_prepares_state_plan_cache_and_optouts(tmp_path: Path) -> None:
    """One startup operation publishes every input needed by discovery."""

    database_path = tmp_path / "index.db"
    repository = DatabaseRepository(DatabaseSettings(database_path))
    repository.write_package(_package("old", "2026-06-28"))
    repository.write_package(_package("current", "2026-06-29"))
    state = StateStore(tmp_path / "state.env")
    state.set_many({"BKG_BATCH_FIRST_STARTED": "2026-06-29", "BKG_OUT": 1})
    cache = OwnerIdentityCache(tmp_path / "owner-id-cache.txt")
    cache.path.write_text("1/stale\n", encoding="utf-8")
    optouts = tmp_path / "optout.txt"
    optouts.write_text('"Alpha"\nAlpha\nenterprise\nBeta\n', encoding="utf-8")
    progress: list[str] = []

    result = _service(database_path, state, cache, progress).prepare(
        RunStartupRequest(
            "2026-06-29",
            1_000,
            tmp_path / "plan",
            database_path,
            optouts,
            "ipitio",
        )
    )

    assert result.batch_first_started == "2026-06-29"
    assert result.package_plan.total == 2
    assert result.package_plan.completed == 1
    assert result.package_plan.pending == 1
    assert result.database_size == database_path.stat().st_size
    assert result.opted_out == 2
    assert result.fast_out
    assert cache.path.read_text(encoding="utf-8") == ""
    assert optouts.read_text(encoding="utf-8") == "Alpha\nBeta\n"
    assert state.get("BKG_SCRIPT_START") == "1000"
    assert state.get("BKG_PACKAGE_PROGRESS_MARKER") == state.get("BKG_BATCH_MARKER")
    assert (tmp_path / "plan" / "packages_to_update").is_file()
    assert progress[0].startswith(
        "Owner queue recovery: active=0 ready=0 claimed=0 paused=0 completed=0 "
        "candidates=0 imported=0 legacy_removed=0 recovered_claims=0 "
        "pruned_stale=0; "
    )
    assert progress[1:] == ["Startup phase 'prepare-package-state' completed in 0s"]


def test_startup_recovers_database_backup_before_planning(tmp_path: Path) -> None:
    """The existing backup fallback remains ahead of lazy schema access."""

    database_path = tmp_path / "index.db"
    backup = Path(f"{database_path}.bak")
    repository = DatabaseRepository(DatabaseSettings(database_path))
    repository.write_package(_package("saved", "2026-06-28"))
    database_path.replace(backup)
    state = StateStore(tmp_path / "state.env")
    cache = OwnerIdentityCache(tmp_path / "owner-id-cache.txt")

    result = _service(database_path, state, cache, []).prepare(
        RunStartupRequest(
            "2026-06-29",
            1_000,
            tmp_path / "plan",
            database_path,
            tmp_path / "optout.txt",
            "fork-owner",
        )
    )

    assert result.package_plan.total == 1
    assert result.package_plan.pending == 1
    assert database_path.is_file()
    assert not backup.exists()
    assert not result.fast_out


def test_startup_seeds_catalog_once_per_index_revision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Startup atomically imports changed trees and skips unchanged enumeration."""

    database_path = tmp_path / "index.db"
    state = StateStore(tmp_path / "state.env")
    cache = OwnerIdentityCache(tmp_path / "owner-id-cache.txt")
    progress: list[str] = []
    service = _service(database_path, state, cache, progress)
    revision = "a" * 40
    paths = (
        PackageCatalogPath("Alpha", "one", "one"),
        PackageCatalogPath("Beta", "two", "two"),
    )
    known_revisions: list[str | None] = []

    def read_catalog(
        _path: Path,
        known_revision: str | None = None,
    ) -> IndexPackageCatalogTree:
        known_revisions.append(known_revision)
        return IndexPackageCatalogTree(
            revision,
            () if known_revision == revision else paths,
        )

    monkeypatch.setattr(run_startup, "read_index_package_catalog", read_catalog)
    request = RunStartupRequest(
        "2026-06-29",
        1_000,
        tmp_path / "plan",
        database_path,
        tmp_path / "optout.txt",
        "fork-owner",
        tmp_path / "index",
    )

    service.prepare(request)
    service.prepare(request)

    assert known_revisions == [None, revision]
    assert service.services.repository.package_inventory() == PackageInventory(2, 2, 2)
    assert any(
        message.startswith("Package catalog initialized: ") for message in progress
    )
    assert any(message.startswith("Package catalog ready: ") for message in progress)


def test_startup_imports_legacy_owner_queue_only_once(tmp_path: Path) -> None:
    """The compatibility key seeds an empty generation but cannot rewrite it later."""

    database_path = tmp_path / "index.db"
    state = StateStore(tmp_path / "state.env")
    state.set_many(
        {
            "BKG_BATCH_MARKER": "batch-1",
            "BKG_OWNERS_QUEUE": r"1/Alpha\n2/Beta",
        }
    )
    cache = OwnerIdentityCache(tmp_path / "owner-id-cache.txt")
    service = _service(database_path, state, cache, [])
    request = RunStartupRequest(
        "2026-06-29",
        1_000,
        tmp_path / "plan",
        database_path,
        tmp_path / "optout.txt",
        "fork-owner",
    )

    service.prepare(request)
    assert state.get("BKG_OWNERS_QUEUE") is None

    state.replace_set("BKG_OWNERS_QUEUE", ("3/Replacement",))
    service.prepare(request)

    assert state.get("BKG_OWNERS_QUEUE") is None
    repository = DatabaseRepository(DatabaseSettings(database_path))
    assert tuple(entry.ref for entry in repository.owner_queue_entries("batch-1")) == (
        "1/Alpha",
        "2/Beta",
    )


def test_startup_retains_legacy_queue_until_database_import_commits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An interrupted cutover leaves the one-time migration input retryable."""

    database_path = tmp_path / "index.db"
    state = StateStore(tmp_path / "state.env")
    state.set_many(
        {
            "BKG_BATCH_MARKER": "batch-1",
            "BKG_OWNERS_QUEUE": r"1/Alpha\n2/Beta",
        }
    )
    cache = OwnerIdentityCache(tmp_path / "owner-id-cache.txt")
    service = _service(database_path, state, cache, [])

    def interrupt_import(*_args: object) -> tuple[object, ...]:
        raise RuntimeError("interrupted import")

    monkeypatch.setattr(
        service.services.repository,
        "prepare_owner_queue",
        interrupt_import,
    )

    with pytest.raises(RuntimeError, match="interrupted import"):
        service.prepare(
            RunStartupRequest(
                "2026-06-29",
                1_000,
                tmp_path / "plan",
                database_path,
                tmp_path / "optout.txt",
                "fork-owner",
            )
        )

    assert state.get_set("BKG_OWNERS_QUEUE") == ["1/Alpha", "2/Beta"]
