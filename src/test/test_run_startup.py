"""Tests for Python-owned application startup preparation."""

from __future__ import annotations

from pathlib import Path

from bkg_py.database import DatabaseRepository, DatabaseSettings
from bkg_py.database_models import PackageRecord, PackageRef
from bkg_py.discovery import OwnerIdentityCache
from bkg_py.run_startup import (
    RunStartupExecution,
    RunStartupRequest,
    RunStartupService,
    RunStartupServices,
)
from bkg_py.snapshots import SnapshotPaths, SnapshotStore
from bkg_py.state import StateStore


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
    assert progress == ["Startup phase 'prepare-package-state' completed in 0s"]


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
