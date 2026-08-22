"""Tests for local database snapshot storage."""

from __future__ import annotations

import sqlite3
import stat
from compression import zstd
from pathlib import Path

import httpx
import pytest

from bkg_py.application import ApplicationContext
from bkg_py.config import RuntimeConfig
from bkg_py.github import GitHubClient, GitHubSettings
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
    with zstd.open(path, "wt", encoding="utf-8") as archive:
        archive.write(content)


def _config(tmp_path: Path, *, index_db: str | None) -> RuntimeConfig:
    return RuntimeConfig(
        github_owner="ipitio",
        github_repo="backage",
        github_branch=None,
        root=str(tmp_path),
        env_file=str(tmp_path / "env.env"),
        owners_file=str(tmp_path / "owners.txt"),
        optout_file=str(tmp_path / "optout.txt"),
        owner_id_cache_file=str(tmp_path / "owner-id-cache.txt"),
        owners_table="owners",
        packages_table="packages",
        versions_table="versions",
        mode=0,
        max_len=14400,
        is_first="false",
        index_name=None,
        index_db=index_db,
        index_sql=None,
        index_dir=None,
    )


def _github_client(
    handler: httpx.MockTransport,
    auth_value: str = "",
) -> GitHubClient:
    return GitHubClient(
        GitHubSettings(token=auth_value, user_agent="test-agent"),
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


def test_unhealthy_release_cleanup_stops_at_supported_snapshot(tmp_path: Path) -> None:
    """Recovery deletes only the leading releases that cannot restore the DB."""

    paths = SnapshotPaths(tmp_path / "index.db")
    store = SnapshotStore(paths)
    requests: list[tuple[str, str]] = []
    latest_reads = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal latest_reads
        requests.append((request.method, request.url.path))
        if request.method == "DELETE":
            return httpx.Response(204)
        latest_reads += 1
        if latest_reads == 1:
            return httpx.Response(
                200,
                json={"id": 42, "tag_name": "v1", "assets": []},
            )
        return httpx.Response(
            200,
            json={
                "id": 41,
                "tag_name": "v0",
                "assets": [
                    {
                        "name": "index.db",
                        "browser_download_url": "https://objects.example/index.db",
                    }
                ],
            },
        )

    messages: list[str] = []
    with _github_client(httpx.MockTransport(respond), "token") as client:
        deleted = store.delete_unhealthy_releases(
            client,
            owner="example",
            repo="bkg",
            progress=messages.append,
        )

    assert deleted == 1
    assert requests == [
        ("GET", "/repos/example/bkg/releases/latest"),
        ("DELETE", "/repos/example/bkg/releases/42"),
        ("GET", "/repos/example/bkg/releases/latest"),
    ]
    assert messages == ["Deleting the latest release..."]


def test_missing_release_snapshot_asset_is_nonfatal(tmp_path: Path) -> None:
    """Releases without supported snapshot assets report absence."""

    store = SnapshotStore(SnapshotPaths(tmp_path / "index.db"))

    assert (
        store.release_snapshot_asset_from_metadata(
            {"assets": [{"name": "notes.txt", "browser_download_url": "https://x"}]}
        )
        is None
    )


def test_unhealthy_release_cleanup_preserves_rotation_archives(tmp_path: Path) -> None:
    """Recovery cannot delete the sole retained copy of historical data."""

    store = SnapshotStore(SnapshotPaths(tmp_path / "index.db"))
    requests: list[tuple[str, str]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        return httpx.Response(
            200,
            json={
                "id": 42,
                "tag_name": "v2026.7.1",
                "assets": [
                    {
                        "name": "2026.07.16T01.02.03.000004Z.index.db.zst",
                    }
                ],
            },
        )

    with (
        _github_client(httpx.MockTransport(respond), "token") as client,
        pytest.raises(SnapshotError, match="historical database rotation archive"),
    ):
        store.delete_unhealthy_releases(
            client,
            owner="example",
            repo="bkg",
        )

    assert requests == [("GET", "/repos/example/bkg/releases/latest")]


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
        rotation_stamp="2026.06.16T12.34.56.000789Z",
    )

    assert result.rotated
    assert result.archive == (
        paths.snapshot_directory / "2026.06.16T12.34.56.000789Z.index.db.zst"
    )
    assert result.archive is not None
    assert result.archive.is_file()
    assert result.source_bytes == paths.current_db_archive.stat().st_size
    assert result.compressed_bytes == result.archive.stat().st_size
    assert paths.current_db_archive.is_file()
    assert prune_calls == 1


def test_first_rotation_archives_live_database_before_pruning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deployment without a prior snapshot still retains pre-prune history."""

    paths = SnapshotPaths(tmp_path / "index.db")
    _create_database(paths.index_db)
    store = SnapshotStore(paths)
    sources: list[Path] = []

    def compress(source: Path, destination: Path) -> None:
        sources.append(source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes())

    monkeypatch.setattr(store, "_compress_zstd_file", compress)

    result = store.rotate_database_if_needed(
        lambda: None,
        threshold_bytes=1,
        rotation_stamp="2026.06.16T12.34.56.000789Z",
    )

    assert sources == [paths.index_db]
    assert result.archive is not None
    assert result.archive.read_bytes() == paths.index_db.read_bytes()
    assert result.source_bytes == paths.index_db.stat().st_size


def test_interrupted_rotation_preserves_existing_archive(tmp_path: Path) -> None:
    """A stop cannot replace a completed rotation archive with partial output."""

    paths = SnapshotPaths(tmp_path / "index.db")
    paths.current_db_archive.parent.mkdir()
    _create_database(paths.current_db_archive)
    archive = paths.snapshot_directory / "2026.06.16T12.34.56.000789Z.index.db.zst"
    archive.write_bytes(b"existing archive")

    def stop() -> None:
        raise GracefulStop("test")

    store = SnapshotStore(paths, check_stop=stop)

    with pytest.raises(GracefulStop):
        store.archive_current_snapshot_for_rotation("2026.06.16T12.34.56.000789Z")

    assert archive.read_bytes() == b"existing archive"
    assert not list(
        paths.snapshot_directory.glob(".2026.06.16T12.34.56.000789Z.*.index.db.zst.*")
    )


def test_rotation_archive_names_do_not_replace_same_timestamp_or_legacy_asset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Timestamp collisions gain a sequence while dated assets remain untouched."""

    paths = SnapshotPaths(tmp_path / "index.db")
    paths.current_db_archive.parent.mkdir()
    paths.current_db_archive.write_bytes(b"current")
    legacy = paths.snapshot_directory / "2026.06.16.index.db.zst"
    collision = paths.snapshot_directory / "2026.06.16T12.34.56.000789Z.index.db.zst"
    legacy.write_bytes(b"legacy")
    collision.write_bytes(b"first")
    store = SnapshotStore(paths)

    def compress(source: Path, destination: Path) -> None:
        destination.write_bytes(source.read_bytes())

    monkeypatch.setattr(store, "_compress_zstd_file", compress)

    archive = store.archive_current_snapshot_for_rotation("2026.06.16T12.34.56.000789Z")

    assert archive is not None
    assert archive == (
        paths.snapshot_directory / "2026.06.16T12.34.56.000789Z.2.index.db.zst"
    )
    assert archive.read_bytes() == b"current"
    assert collision.read_bytes() == b"first"
    assert legacy.read_bytes() == b"legacy"


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
    value = f"{'x' * (1024 * 1024)};from sql"
    _write_zstd_archive(
        paths.legacy_sql_archive,
        "begin; create table payload (value text);\n"
        f"insert into payload (value) values ('{value}'); commit;\n",
    )

    result = store.restore_database_if_needed()

    assert result is not None
    assert result.restored
    assert result.message == "Restoring database from legacy index.sql.zst..."
    assert _read_payload(paths.index_db) == value


def test_restore_legacy_compressed_database_snapshot(tmp_path: Path) -> None:
    """Legacy compressed databases stream through the standard-library codec."""

    paths = SnapshotPaths(tmp_path / "index.db")
    store = SnapshotStore(paths)
    source = tmp_path / "source.db"
    _create_database(source)
    with (
        source.open("rb") as database,
        zstd.open(
            paths.legacy_db_archive,
            "wb",
        ) as archive,
    ):
        while chunk := database.read(1024 * 1024):
            archive.write(chunk)

    result = store.restore_database_if_needed()

    assert result is not None
    assert result.restored
    assert result.message == "Restoring database from index.db.zst..."
    assert _read_payload(paths.index_db) == "stored"


def test_invalid_legacy_sql_never_replaces_existing_database(tmp_path: Path) -> None:
    """A failed streaming SQL import leaves the current database untouched."""

    paths = SnapshotPaths(tmp_path / "index.db", index_sql=tmp_path / "index.sql")
    store = SnapshotStore(paths)
    _create_database(paths.index_db)
    original_database = paths.index_db.read_bytes()
    _write_zstd_archive(
        paths.legacy_sql_archive,
        "begin; create table replacement (value); invalid syntax; commit;",
    )

    with pytest.raises(SnapshotError, match="legacy SQL restore failed"):
        store.restore_database_if_needed()

    assert paths.index_db.read_bytes() == original_database
    assert not paths.restore_signature.exists()
    assert not list(paths.index_db.parent.glob(".index.db.*"))
