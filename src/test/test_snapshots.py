"""Tests for local database snapshot storage."""

from __future__ import annotations

import shutil
import sqlite3
import stat
import subprocess
from pathlib import Path

import httpx
import pytest

from bkg_py.application import ApplicationContext
from bkg_py.cli import main
from bkg_py.config import RuntimeConfig
from bkg_py.github import GitHubClient, GitHubSettings
from bkg_py.result import ExitStatus
from bkg_py.runtime import GracefulStop
from bkg_py.snapshots import (
    SnapshotError,
    SnapshotPaths,
    SnapshotStore,
    sha256_file,
)


def _create_database(path: Path) -> None:
    with sqlite3.connect(path) as database:
        database.execute("pragma journal_mode = wal")
        database.execute("create table payload (value text)")
        database.execute("insert into payload (value) values ('stored')")
    with sqlite3.connect(path, isolation_level=None) as database:
        database.execute("pragma wal_checkpoint(truncate)")


def _read_payload(path: Path) -> str:
    with sqlite3.connect(path) as database:
        row = database.execute("select value from payload").fetchone()
    assert row is not None
    return str(row[0])


def _write_zstd_archive(path: Path, content: str) -> None:
    zstd = shutil.which("zstd")
    assert zstd is not None
    subprocess.run(  # noqa: S603
        (zstd, "-q", "-f", "-o", str(path)),
        input=content,
        text=True,
        check=True,
    )


def _config(tmp_path: Path, *, index_db: str | None) -> RuntimeConfig:
    return RuntimeConfig(
        github_owner="ipitio",
        github_repo="backage",
        github_branch=None,
        root=str(tmp_path),
        env_file=str(tmp_path / "env.env"),
        owners_file=str(tmp_path / "owners.txt"),
        optout_file=str(tmp_path / "optout.txt"),
        owners_table="owners",
        packages_table="packages",
        versions_table="versions",
        mode=0,
        max_len=14400,
        is_first="false",
        page_all=1,
        index_name=None,
        index_db=index_db,
        index_sql=None,
        index_dir=None,
    )


def _set_snapshot_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    paths: SnapshotPaths,
) -> None:
    monkeypatch.setenv("BKG_ROOT", str(tmp_path))
    monkeypatch.setenv("BKG_ENV", str(tmp_path / "env.env"))
    monkeypatch.setenv("BKG_INDEX_DB", str(paths.index_db))
    if paths.index_sql is not None:
        monkeypatch.setenv("BKG_INDEX_SQL", str(paths.index_sql))


def _github_client(
    handler: httpx.MockTransport,
    auth_value: str = "",
) -> GitHubClient:
    return GitHubClient(
        GitHubSettings(token=auth_value),
        client=httpx.Client(transport=handler),
    )


def test_snapshot_paths_match_shell_layout(tmp_path: Path) -> None:
    """Snapshot paths match the Bash archive naming convention."""

    paths = SnapshotPaths(tmp_path / "index.db", index_sql=tmp_path / "index.sql")

    assert paths.current_db_archive == tmp_path / ".snapshot" / "index.db"
    assert paths.current_db_asset_name == "index.db"
    assert paths.legacy_db_archive == tmp_path / "index.db.zst"
    assert paths.legacy_db_asset_name == "index.db.zst"
    assert paths.legacy_sql_archive == tmp_path / "index.sql.zst"
    assert paths.legacy_sql_asset_name == "index.sql.zst"
    assert paths.restore_signature == tmp_path / "index.db.snapshot.sha256"


def test_snapshot_store_uses_runtime_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The application context exposes snapshot storage lazily."""

    monkeypatch.setenv("BKG_ROOT", str(tmp_path))
    monkeypatch.setenv("BKG_ENV", str(tmp_path / "env.env"))
    monkeypatch.setenv("BKG_INDEX", "index-feature")

    application = ApplicationContext.from_env()

    assert application.snapshots is application.snapshots
    assert application.snapshots.paths.index_db == tmp_path / "index-feature.db"


def test_snapshot_paths_require_database_config(tmp_path: Path) -> None:
    """Snapshot storage reports a missing database path when used."""

    with pytest.raises(SnapshotError, match="BKG_INDEX_DB is required"):
        SnapshotPaths.from_config(_config(tmp_path, index_db=None))


def test_current_archive_prefers_current_database_snapshot(tmp_path: Path) -> None:
    """Current uncompressed archives win over legacy compatibility files."""

    paths = SnapshotPaths(tmp_path / "index.db", index_sql=tmp_path / "index.sql")
    store = SnapshotStore(paths)
    paths.current_db_archive.parent.mkdir()
    paths.legacy_db_archive.write_bytes(b"legacy db")
    paths.legacy_sql_archive.write_bytes(b"legacy sql")
    paths.current_db_archive.write_bytes(b"current db")

    archive = store.current_archive()

    assert archive is not None
    assert archive.path == paths.current_db_archive
    assert archive.kind == "db"


def test_restore_signature_requires_existing_database(tmp_path: Path) -> None:
    """A matching archive digest is not enough without a local database."""

    paths = SnapshotPaths(tmp_path / "index.db")
    store = SnapshotStore(paths)
    paths.current_db_archive.parent.mkdir()
    paths.current_db_archive.write_bytes(b"snapshot")
    paths.restore_signature.write_text(
        f"{sha256_file(paths.current_db_archive)}\n",
        encoding="utf-8",
    )

    assert not store.restore_signature_matches()

    paths.index_db.write_bytes(b"database")
    assert store.restore_signature_matches()


def test_prepare_database_snapshot_is_atomic_and_removes_legacy(
    tmp_path: Path,
) -> None:
    """A prepared snapshot is checkpointed, signed, and replaces legacy files."""

    paths = SnapshotPaths(tmp_path / "index.db", index_sql=tmp_path / "index.sql")
    store = SnapshotStore(paths)
    _create_database(paths.index_db)
    paths.legacy_db_archive.write_bytes(b"legacy db")
    paths.legacy_sql_archive.write_bytes(b"legacy sql")

    archive = store.prepare_database_snapshot()

    assert archive == paths.current_db_archive
    assert not paths.legacy_db_archive.exists()
    assert not paths.legacy_sql_archive.exists()
    assert paths.restore_signature.read_text(encoding="utf-8").strip() == sha256_file(
        archive
    )
    assert stat.S_IMODE(archive.stat().st_mode) == 0o666
    with sqlite3.connect(archive) as database:
        row = database.execute("select value from payload").fetchone()
    assert row == ("stored",)


def test_release_snapshot_asset_prefers_current_archive(tmp_path: Path) -> None:
    """Release asset selection follows the snapshot compatibility order."""

    paths = SnapshotPaths(tmp_path / "index.db", index_sql=tmp_path / "index.sql")
    store = SnapshotStore(paths)

    asset = store.release_snapshot_asset_from_metadata(
        {
            "assets": [
                {
                    "name": "index.sql.zst",
                    "browser_download_url": "https://objects.example/index.sql.zst",
                },
                {
                    "name": "index.db",
                    "url": (
                        "https://api.github.com/repos/ipitio/backage/releases/assets/7"
                    ),
                    "browser_download_url": "https://objects.example/index.db",
                },
            ]
        }
    )

    assert asset is not None
    assert asset.name == "index.db"
    assert asset.archive.kind == "db"
    assert asset.archive.path == paths.current_db_archive
    assert asset.authenticated


def test_missing_release_snapshot_asset_is_nonfatal(tmp_path: Path) -> None:
    """Releases without supported snapshot assets report absence."""

    store = SnapshotStore(SnapshotPaths(tmp_path / "index.db"))

    assert (
        store.release_snapshot_asset_from_metadata(
            {"assets": [{"name": "notes.txt", "browser_download_url": "https://x"}]}
        )
        is None
    )


def test_release_snapshot_asset_requires_download_url(tmp_path: Path) -> None:
    """A matching release asset must include a usable download URL."""

    store = SnapshotStore(SnapshotPaths(tmp_path / "index.db"))

    with pytest.raises(SnapshotError, match="no download URL"):
        store.release_snapshot_asset_from_metadata({"assets": [{"name": "index.db"}]})


def test_download_release_snapshot_restores_database_and_prunes_stale_archives(
    tmp_path: Path,
) -> None:
    """Release downloads use Python HTTP and restore one canonical archive."""

    paths = SnapshotPaths(tmp_path / "index.db", index_sql=tmp_path / "index.sql")
    store = SnapshotStore(paths)
    source_database = tmp_path / "source.db"
    _create_database(source_database)
    paths.legacy_db_archive.write_bytes(b"stale legacy")
    requests: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if request.url.path == "/repos/ipitio/backage/releases/latest":
            return httpx.Response(
                200,
                json={
                    "assets": [
                        {
                            "name": "index.db",
                            "url": (
                                "https://api.github.com/repos/ipitio/backage/"
                                "releases/assets/7"
                            ),
                            "browser_download_url": "https://objects.example/index.db",
                        }
                    ]
                },
            )
        assert (
            request.url
            == "https://api.github.com/repos/ipitio/backage/releases/assets/7"
        )
        assert request.headers["authorization"] == "Bearer test-token"
        assert request.headers["accept"] == "application/octet-stream"
        return httpx.Response(200, content=source_database.read_bytes())

    client = _github_client(httpx.MockTransport(respond), "test-token")
    asset = store.release_snapshot_asset(client, owner="ipitio", repo="backage")

    assert asset is not None
    result = store.download_release_snapshot(client, asset)

    assert result.restored
    assert result.message == "Restoring database from index.db..."
    assert _read_payload(paths.index_db) == "stored"
    assert paths.current_db_archive.is_file()
    assert not paths.legacy_db_archive.exists()
    assert requests == [
        "https://api.github.com/repos/ipitio/backage/releases/latest",
        "https://api.github.com/repos/ipitio/backage/releases/assets/7",
    ]


def test_interrupted_snapshot_preserves_existing_archive(tmp_path: Path) -> None:
    """A stop during copy leaves the previous complete archive intact."""

    paths = SnapshotPaths(tmp_path / "index.db")
    paths.current_db_archive.parent.mkdir()
    paths.current_db_archive.write_bytes(b"old archive")
    _create_database(paths.index_db)

    def stop() -> None:
        raise GracefulStop("test")

    store = SnapshotStore(paths, check_stop=stop)

    with pytest.raises(GracefulStop):
        store.prepare_database_snapshot()

    assert paths.current_db_archive.read_bytes() == b"old archive"
    assert not list(paths.current_db_archive.parent.glob(".index.db.*"))


def test_rotate_database_archives_current_snapshot_and_prunes(
    tmp_path: Path,
) -> None:
    """Oversized working databases archive the previous snapshot before pruning."""

    paths = SnapshotPaths(tmp_path / "index.db")
    store = SnapshotStore(paths)
    prune_calls = 0
    paths.current_db_archive.parent.mkdir()
    _create_database(paths.index_db)
    _create_database(paths.current_db_archive)

    def prune_database() -> None:
        nonlocal prune_calls
        prune_calls += 1

    result = store.rotate_database_if_needed(
        prune_database,
        threshold_bytes=1,
        date_stamp="2026.06.16",
    )

    assert result.rotated
    assert result.archive == paths.snapshot_directory / "2026.06.16.index.db.zst"
    assert result.archive is not None
    assert result.archive.is_file()
    assert paths.current_db_archive.is_file()
    assert prune_calls == 1


def test_restore_current_database_snapshot_replaces_after_validation(
    tmp_path: Path,
) -> None:
    """Current DB archives are validated before replacing the working DB."""

    paths = SnapshotPaths(tmp_path / "index.db")
    store = SnapshotStore(paths)
    paths.current_db_archive.parent.mkdir()
    _create_database(paths.current_db_archive)
    paths.index_db.write_bytes(b"old non-sqlite content")

    result = store.restore_database_if_needed()

    assert result is not None
    assert result.restored
    assert result.message == "Restoring database from index.db..."
    assert _read_payload(paths.index_db) == "stored"
    assert paths.restore_signature.read_text(encoding="utf-8").strip() == sha256_file(
        paths.current_db_archive
    )


def test_restore_matching_snapshot_signature_skips_existing_database(
    tmp_path: Path,
) -> None:
    """Matching signatures skip work and leave the existing DB untouched."""

    paths = SnapshotPaths(tmp_path / "index.db")
    store = SnapshotStore(paths)
    paths.current_db_archive.parent.mkdir()
    _create_database(paths.current_db_archive)
    paths.index_db.write_bytes(b"existing")
    paths.restore_signature.write_text(
        f"{sha256_file(paths.current_db_archive)}\n",
        encoding="utf-8",
    )

    result = store.restore_database_if_needed()

    assert result is not None
    assert not result.restored
    assert result.message == "Using existing database; index.db unchanged"
    assert paths.index_db.read_bytes() == b"existing"


def test_corrupt_restore_never_replaces_existing_database(tmp_path: Path) -> None:
    """Invalid archives fail before replacing a usable local database."""

    paths = SnapshotPaths(tmp_path / "index.db")
    store = SnapshotStore(paths)
    paths.current_db_archive.parent.mkdir()
    paths.current_db_archive.write_bytes(b"not sqlite")
    _create_database(paths.index_db)
    original_database = paths.index_db.read_bytes()

    with pytest.raises(SnapshotError, match="invalid restored database"):
        store.restore_database_if_needed()

    assert paths.index_db.read_bytes() == original_database
    assert not paths.restore_signature.exists()
    assert not list(paths.index_db.parent.glob(".index.db.*"))


def test_restore_legacy_sql_snapshot_imports_into_temporary_database(
    tmp_path: Path,
) -> None:
    """Legacy SQL archives import into a temp DB before replacement."""

    paths = SnapshotPaths(tmp_path / "index.db", index_sql=tmp_path / "index.sql")
    store = SnapshotStore(paths)
    _write_zstd_archive(
        paths.legacy_sql_archive,
        """
        create table payload (value text);
        insert into payload (value) values ('from sql');
        """,
    )

    result = store.restore_database_if_needed()

    assert result is not None
    assert result.restored
    assert result.message == "Restoring database from legacy index.sql.zst..."
    assert _read_payload(paths.index_db) == "from sql"


def test_snapshot_cli_exposes_shell_shaped_archive_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Snapshot CLI commands print values and report booleans by status."""

    paths = SnapshotPaths(tmp_path / "index.db", index_sql=tmp_path / "index.sql")
    _set_snapshot_env(monkeypatch, tmp_path, paths)
    paths.current_db_archive.parent.mkdir()
    paths.current_db_archive.write_bytes(b"snapshot")

    assert main(["snapshot", "current-archive"]) == ExitStatus.SUCCESS
    assert capsys.readouterr().out.strip() == str(paths.current_db_archive)

    assert main(["snapshot", "path", "db"]) == ExitStatus.SUCCESS
    assert capsys.readouterr().out.strip() == str(paths.current_db_archive)

    assert main(["snapshot", "path", "db-zst"]) == ExitStatus.SUCCESS
    assert capsys.readouterr().out.strip() == str(paths.legacy_db_archive)

    assert main(["snapshot", "path", "sql-zst"]) == ExitStatus.SUCCESS
    assert capsys.readouterr().out.strip() == str(paths.legacy_sql_archive)

    assert main(["snapshot", "path", "restore-signature"]) == ExitStatus.SUCCESS
    assert capsys.readouterr().out.strip() == str(paths.restore_signature)

    assert main(["snapshot", "asset-name", "db"]) == ExitStatus.SUCCESS
    assert capsys.readouterr().out.strip() == paths.current_db_asset_name

    assert main(["snapshot", "asset-name", "db-zst"]) == ExitStatus.SUCCESS
    assert capsys.readouterr().out.strip() == paths.legacy_db_asset_name

    assert main(["snapshot", "asset-name", "sql-zst"]) == ExitStatus.SUCCESS
    assert capsys.readouterr().out.strip() == paths.legacy_sql_asset_name

    assert main(["snapshot", "current-signature"]) == ExitStatus.SUCCESS
    assert capsys.readouterr().out.strip() == sha256_file(paths.current_db_archive)

    assert main(["snapshot", "restore-signature-matches"]) == ExitStatus.NON_FATAL
    assert capsys.readouterr().out == ""

    paths.index_db.write_bytes(b"database")
    assert main(["snapshot", "write-restore-signature"]) == ExitStatus.SUCCESS
    assert capsys.readouterr().out == ""

    assert main(["snapshot", "restore-signature-matches"]) == ExitStatus.SUCCESS
    assert capsys.readouterr().out == ""


def test_snapshot_cli_reports_missing_current_archive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Direct archive probes explain a missing snapshot."""

    paths = SnapshotPaths(tmp_path / "index.db")
    _set_snapshot_env(monkeypatch, tmp_path, paths)

    assert main(["snapshot", "current-archive"]) == ExitStatus.NON_FATAL

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip() == "No database snapshot archive found"


def test_snapshot_cli_reports_graceful_stop_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Status 3 includes the stop reason in Action logs."""

    paths = SnapshotPaths(tmp_path / "index.db")
    _set_snapshot_env(monkeypatch, tmp_path, paths)
    (tmp_path / "env.env").write_text("BKG_TIMEOUT=1\n", encoding="utf-8")

    assert main(["snapshot", "current-archive"]) == ExitStatus.GRACEFUL_STOP

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip() == "Graceful stop requested: persisted"


def test_snapshot_cli_restores_database_if_needed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The restore command prints the shell-compatible restore message."""

    paths = SnapshotPaths(tmp_path / "index.db")
    _set_snapshot_env(monkeypatch, tmp_path, paths)
    paths.current_db_archive.parent.mkdir()
    _create_database(paths.current_db_archive)

    assert main(["snapshot", "restore-if-needed"]) == ExitStatus.SUCCESS

    assert capsys.readouterr().out.strip() == "Restoring database from index.db..."
    assert _read_payload(paths.index_db) == "stored"


def test_snapshot_cli_restores_explicit_archive_if_needed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The startup restore command restores the archive selected by the shell."""

    paths = SnapshotPaths(tmp_path / "index.db")
    _set_snapshot_env(monkeypatch, tmp_path, paths)
    paths.current_db_archive.parent.mkdir()
    _create_database(paths.current_db_archive)

    assert (
        main(
            [
                "snapshot",
                "restore-archive-if-needed",
                str(paths.current_db_archive),
            ]
        )
        == ExitStatus.SUCCESS
    )

    assert capsys.readouterr().out.strip() == "Restoring database from index.db..."
    assert _read_payload(paths.index_db) == "stored"


def test_snapshot_cli_prepares_archive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The prepare command publishes a checkpointed current archive."""

    paths = SnapshotPaths(tmp_path / "index.db", index_sql=tmp_path / "index.sql")
    _set_snapshot_env(monkeypatch, tmp_path, paths)
    _create_database(paths.index_db)
    paths.legacy_db_archive.write_bytes(b"legacy db")
    paths.legacy_sql_archive.write_bytes(b"legacy sql")

    assert main(["snapshot", "prepare"]) == ExitStatus.SUCCESS
    assert capsys.readouterr().out.strip() == str(paths.current_db_archive)
    assert paths.current_db_archive.is_file()
    assert not paths.legacy_db_archive.exists()
    assert not paths.legacy_sql_archive.exists()
