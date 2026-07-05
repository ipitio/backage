"""Database snapshot archive selection and atomic local storage."""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Literal, cast
from urllib.parse import quote

from .config import RuntimeConfig
from .files import atomic_binary_output, atomic_path, atomic_text_output
from .github import GitHubClient

ArchiveKind = Literal["db", "db-zst", "sql-zst"]
StopCheck = Callable[[], None]
_COPY_CHUNK_SIZE = 1024 * 1024
_DATABASE_MODE = 0o666
_PROCESS_KILL_TIMEOUT_SECONDS = 5
_PROCESS_POLL_SECONDS = 0.25
_RELEASE_ASSET_KINDS: tuple[ArchiveKind, ...] = ("db", "db-zst", "sql-zst")
_SNAPSHOT_MODE = 0o666


class SnapshotError(RuntimeError):
    """Snapshot storage cannot preserve or restore the database safely."""


@dataclass(frozen=True)
class SnapshotArchive:
    """A candidate database snapshot archive and its compatibility shape."""

    path: Path
    kind: ArchiveKind


@dataclass(frozen=True)
class ReleaseSnapshotAsset:
    """A release asset that can restore the configured database snapshot."""

    archive: SnapshotArchive
    name: str
    download_url: str
    authenticated: bool


@dataclass(frozen=True)
class SnapshotRestoreResult:
    """The user-facing result of one local snapshot restore check."""

    archive: SnapshotArchive
    restored: bool
    message: str


@dataclass(frozen=True)
class SnapshotRotationResult:
    """The result of rotating an oversized database before publication."""

    rotated: bool
    archive: Path | None = None


@dataclass(frozen=True)
class SnapshotPaths:
    """Filesystem paths that make up one index database snapshot family."""

    index_db: Path
    index_sql: Path | None = None
    snapshot_dir: Path | None = None

    @classmethod
    def from_config(cls, config: RuntimeConfig) -> SnapshotPaths:
        """Build snapshot paths from the shell-compatible runtime config."""

        if config.index_db is None:
            raise SnapshotError("BKG_INDEX_DB is required")
        index_sql = Path(config.index_sql) if config.index_sql else None
        return cls(Path(config.index_db), index_sql=index_sql)

    @property
    def snapshot_directory(self) -> Path:
        """Return the current uncompressed archive directory."""

        return self.snapshot_dir or self.index_db.parent / ".snapshot"

    @property
    def current_db_archive(self) -> Path:
        """Return the current uncompressed database archive path."""

        return self.snapshot_directory / self.index_db.name

    @property
    def current_db_asset_name(self) -> str:
        """Return the current uncompressed database release asset name."""

        return self.current_db_archive.name

    @property
    def legacy_db_archive(self) -> Path:
        """Return the legacy compressed database archive path."""

        return Path(f"{self.index_db}.zst")

    @property
    def legacy_db_asset_name(self) -> str:
        """Return the legacy compressed database release asset name."""

        return self.legacy_db_archive.name

    @property
    def legacy_sql_archive(self) -> Path:
        """Return the legacy compressed SQL dump archive path."""

        if self.index_sql is not None:
            return Path(f"{self.index_sql}.zst")
        return self.index_db.with_suffix(".sql.zst")

    @property
    def legacy_sql_asset_name(self) -> str:
        """Return the legacy compressed SQL dump release asset name."""

        return self.legacy_sql_archive.name

    @property
    def restore_signature(self) -> Path:
        """Return the checksum file for the last restored archive."""

        return Path(f"{self.index_db}.snapshot.sha256")


class SnapshotStore:
    """Own local database snapshot files without changing live workflows yet."""

    def __init__(
        self,
        paths: SnapshotPaths,
        *,
        check_stop: StopCheck = lambda: None,
    ) -> None:
        self.paths = paths
        self._check_stop = check_stop

    @classmethod
    def from_config(
        cls,
        config: RuntimeConfig,
        *,
        check_stop: StopCheck = lambda: None,
    ) -> SnapshotStore:
        """Build a snapshot store from runtime configuration."""

        return cls(SnapshotPaths.from_config(config), check_stop=check_stop)

    def archive_candidates(self) -> tuple[SnapshotArchive, ...]:
        """Return archive candidates in restore-preference order."""

        return (
            SnapshotArchive(self.paths.current_db_archive, "db"),
            SnapshotArchive(self.paths.legacy_db_archive, "db-zst"),
            SnapshotArchive(self.paths.legacy_sql_archive, "sql-zst"),
        )

    def current_archive(self) -> SnapshotArchive | None:
        """Return the first available snapshot archive, if any."""

        for archive in self.archive_candidates():
            self._check_stop()
            if archive.path.is_file():
                return archive
        return None

    def current_signature(self) -> str:
        """Return the SHA-256 digest of the selected snapshot archive."""

        archive = self.current_archive()
        if archive is None:
            raise SnapshotError("no database snapshot archive found")
        return sha256_file(archive.path, self._check_stop)

    def archive_path(self, kind: ArchiveKind | Literal["restore-signature"]) -> Path:
        """Return one configured snapshot path without checking existence."""

        if kind == "db":
            return self.paths.current_db_archive
        if kind == "db-zst":
            return self.paths.legacy_db_archive
        if kind == "sql-zst":
            return self.paths.legacy_sql_archive
        return self.paths.restore_signature

    def asset_name(self, kind: ArchiveKind) -> str:
        """Return one configured snapshot release asset name."""

        if kind == "db":
            return self.paths.current_db_asset_name
        if kind == "db-zst":
            return self.paths.legacy_db_asset_name
        return self.paths.legacy_sql_asset_name

    def restore_signature_matches(self) -> bool:
        """Return whether the local database already matches the archive."""

        archive = self.current_archive()
        if archive is None:
            return False
        return self._restore_signature_matches_value(
            sha256_file(archive.path, self._check_stop)
        )

    def write_restore_signature(self) -> bool:
        """Persist the digest of the selected snapshot archive."""

        archive = self.current_archive()
        if archive is None:
            return False
        self._write_restore_signature_value(sha256_file(archive.path, self._check_stop))
        return True

    def restore_database_if_needed(self) -> SnapshotRestoreResult | None:
        """Restore the selected archive unless the local database already matches."""

        archive = self.current_archive()
        if archive is None:
            return None
        return self.restore_archive_if_needed(archive)

    def restore_archive_if_needed(
        self,
        archive: SnapshotArchive,
    ) -> SnapshotRestoreResult:
        """Restore one explicit archive unless the local database already matches."""

        signature = sha256_file(archive.path, self._check_stop)
        archive_name = archive.path.name
        if self._restore_signature_matches_value(signature):
            return SnapshotRestoreResult(
                archive,
                restored=False,
                message=f"Using existing database; {archive_name} unchanged",
            )

        if archive.kind == "sql-zst":
            message = f"Restoring database from legacy {archive_name}..."
        else:
            message = f"Restoring database from {archive_name}..."
        self._restore_archive(archive)
        self._write_restore_signature_value(signature)
        return SnapshotRestoreResult(archive, restored=True, message=message)

    def restore_archive_path_if_needed(self, path: Path) -> SnapshotRestoreResult:
        """Restore a configured archive path unless the local database matches."""

        archive_path = path.resolve()
        for archive in self.archive_candidates():
            if archive.path.resolve() == archive_path:
                return self.restore_archive_if_needed(archive)
        raise SnapshotError(f"unsupported database snapshot archive: {path}")

    def release_snapshot_asset_from_metadata(
        self,
        release: object,
    ) -> ReleaseSnapshotAsset | None:
        """Select the preferred snapshot asset from GitHub release metadata."""

        if not isinstance(release, Mapping):
            raise SnapshotError("latest release metadata is not an object")
        release_metadata = cast(Mapping[str, object], release)
        assets = release_metadata.get("assets")
        if not isinstance(assets, list):
            return None
        asset_values = cast(list[object], assets)

        for kind in _RELEASE_ASSET_KINDS:
            expected_name = self.asset_name(kind)
            for asset_value in asset_values:
                if not isinstance(asset_value, Mapping):
                    continue
                asset = cast(Mapping[str, object], asset_value)
                if asset.get("name") != expected_name:
                    continue
                download_url = asset.get("url")
                authenticated = True
                if not isinstance(download_url, str) or not download_url:
                    download_url = asset.get("browser_download_url")
                    authenticated = False
                if not isinstance(download_url, str) or not download_url:
                    raise SnapshotError(
                        f"release asset {expected_name} has no download URL"
                    )
                return ReleaseSnapshotAsset(
                    SnapshotArchive(self.archive_path(kind), kind),
                    expected_name,
                    download_url,
                    authenticated,
                )
        return None

    def release_snapshot_asset(
        self,
        client: GitHubClient,
        *,
        owner: str,
        repo: str,
        tag: str | None = None,
    ) -> ReleaseSnapshotAsset | None:
        """Fetch release metadata and return the selected snapshot asset."""

        response = client.rest_json_optional(_release_metadata_path(owner, repo, tag))
        if response is None:
            return None
        return self.release_snapshot_asset_from_metadata(response.value)

    def download_release_snapshot(
        self,
        client: GitHubClient,
        asset: ReleaseSnapshotAsset,
    ) -> SnapshotRestoreResult:
        """Download one release asset and restore it into the working database."""

        client.download(
            asset.download_url,
            asset.archive.path,
            authenticated=asset.authenticated,
            default_mode=_SNAPSHOT_MODE,
        )
        result = self.restore_archive_if_needed(asset.archive)
        for archive in self.archive_candidates():
            self._check_stop()
            if archive.path != asset.archive.path:
                archive.path.unlink(missing_ok=True)
        return result

    def _write_restore_signature_value(self, signature: str) -> None:
        self.paths.restore_signature.parent.mkdir(parents=True, exist_ok=True)
        with atomic_text_output(self.paths.restore_signature) as output:
            output.write(f"{signature}\n")

    def _restore_signature_matches_value(self, signature: str) -> bool:
        if not self.paths.index_db.is_file() or self.paths.index_db.stat().st_size == 0:
            return False
        try:
            stored_signature = self.paths.restore_signature.read_text(
                encoding="utf-8"
            ).strip()
        except FileNotFoundError:
            return False
        return bool(stored_signature) and stored_signature == signature

    def checkpoint_database(self) -> None:
        """Checkpoint the SQLite WAL before copying the database archive."""

        if not self.paths.index_db.exists():
            return
        try:
            with sqlite3.connect(self.paths.index_db, isolation_level=None) as database:
                database.execute("pragma wal_checkpoint(truncate)")
        except sqlite3.Error as error:
            raise SnapshotError(str(error)) from error

    def database_size(self) -> int:
        """Return the live database size, or zero when it does not exist."""

        try:
            return self.paths.index_db.stat().st_size
        except FileNotFoundError:
            return 0

    def prepare_database_snapshot(self) -> Path:
        """Atomically copy the checkpointed database into the current archive."""

        if not self.paths.index_db.is_file():
            raise SnapshotError(f"database does not exist: {self.paths.index_db}")
        self.checkpoint_database()
        self.paths.snapshot_directory.mkdir(parents=True, exist_ok=True)
        self._copy_file_atomic(self.paths.index_db, self.paths.current_db_archive)
        for archive in (self.paths.legacy_db_archive, self.paths.legacy_sql_archive):
            archive.unlink(missing_ok=True)
        self.write_restore_signature()
        return self.paths.current_db_archive

    def rotate_database_if_needed(
        self,
        prune_database: Callable[[], object],
        *,
        threshold_bytes: int,
        date_stamp: str,
    ) -> SnapshotRotationResult:
        """Archive the prior snapshot and prune an oversized working database."""

        self.checkpoint_database()
        if not self.paths.index_db.is_file():
            raise SnapshotError(f"database does not exist: {self.paths.index_db}")
        if self.paths.index_db.stat().st_size < threshold_bytes:
            return SnapshotRotationResult(rotated=False)

        archive = self.archive_current_snapshot_for_rotation(date_stamp)
        prune_database()
        self.checkpoint_database()
        return SnapshotRotationResult(rotated=True, archive=archive)

    def archive_current_snapshot_for_rotation(self, date_stamp: str) -> Path | None:
        """Compress the current snapshot into the dated rotation archive."""

        if not self.paths.current_db_archive.is_file():
            return None
        destination = (
            self.paths.snapshot_directory
            / f"{date_stamp}.{self.paths.current_db_archive.name}.zst"
        )
        destination.unlink(missing_ok=True)
        self._compress_zstd_file(self.paths.current_db_archive, destination)
        return destination

    def _restore_archive(self, archive: SnapshotArchive) -> None:
        self.paths.index_db.parent.mkdir(parents=True, exist_ok=True)
        with atomic_path(
            self.paths.index_db,
            default_mode=_DATABASE_MODE,
        ) as temporary_database:
            if archive.kind == "db":
                self._copy_file(archive.path, temporary_database)
            elif archive.kind == "db-zst":
                self._decompress_zstd_to_file(archive.path, temporary_database)
            else:
                self._import_legacy_sql_archive(archive.path, temporary_database)
            self._validate_database(temporary_database)

    def _copy_file(self, source: Path, destination: Path) -> None:
        with source.open("rb") as source_file, destination.open("wb") as output_file:
            while True:
                self._check_stop()
                chunk = source_file.read(_COPY_CHUNK_SIZE)
                if not chunk:
                    break
                output_file.write(chunk)

    def _copy_file_atomic(self, source: Path, destination: Path) -> None:
        with (
            source.open("rb") as source_file,
            atomic_binary_output(
                destination,
                default_mode=_SNAPSHOT_MODE,
            ) as output_file,
        ):
            while True:
                self._check_stop()
                chunk = source_file.read(_COPY_CHUNK_SIZE)
                if not chunk:
                    break
                output_file.write(chunk)

    def _decompress_zstd_to_file(self, source: Path, destination: Path) -> None:
        with (
            destination.open("wb") as output_file,
            tempfile.TemporaryFile() as stderr,
            subprocess.Popen(  # noqa: S603
                (_required_executable("unzstd"), "-c", str(source)),
                stdout=output_file,
                stderr=stderr,
            ) as process,
        ):
            self._wait_processes((process,))
            if process.returncode != 0:
                raise SnapshotError(_process_error("unzstd", stderr))

    def _compress_zstd_file(self, source: Path, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with (
            tempfile.TemporaryFile() as stderr,
            subprocess.Popen(  # noqa: S603
                (
                    _required_executable("zstd"),
                    "-22",
                    "--ultra",
                    "--long",
                    "-T0",
                    str(source),
                    "-o",
                    str(destination),
                ),
                stderr=stderr,
            ) as process,
        ):
            self._wait_processes((process,))
            if process.returncode != 0:
                destination.unlink(missing_ok=True)
                raise SnapshotError(_process_error("zstd", stderr))

    def _import_legacy_sql_archive(self, source: Path, destination: Path) -> None:
        with (
            tempfile.TemporaryFile() as zstd_stderr,
            tempfile.TemporaryFile() as sqlite_stderr,
            subprocess.Popen(  # noqa: S603
                (_required_executable("unzstd"), "-c", str(source)),
                stdout=subprocess.PIPE,
                stderr=zstd_stderr,
            ) as zstd,
        ):
            if zstd.stdout is None:
                raise SnapshotError("unzstd stdout was not available")
            with subprocess.Popen(  # noqa: S603
                (_required_executable("sqlite3"), str(destination)),
                stdin=zstd.stdout,
                stderr=sqlite_stderr,
            ) as sqlite:
                zstd.stdout.close()
                self._wait_processes((zstd, sqlite))
            if zstd.returncode != 0:
                raise SnapshotError(_process_error("unzstd", zstd_stderr))
            if sqlite.returncode != 0:
                raise SnapshotError(_process_error("sqlite3", sqlite_stderr))

    def _wait_processes(self, processes: Sequence[subprocess.Popen[bytes]]) -> None:
        try:
            while any(process.poll() is None for process in processes):
                self._check_stop()
                time.sleep(_PROCESS_POLL_SECONDS)
        except BaseException:
            for process in processes:
                _terminate_process(process)
            raise
        self._check_stop()

    def _validate_database(self, path: Path) -> None:
        try:
            with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as database:
                database.execute("select name from sqlite_master limit 1").fetchone()
        except sqlite3.Error as error:
            raise SnapshotError(f"invalid restored database: {error}") from error


def sha256_file(path: Path, check_stop: StopCheck = lambda: None) -> str:
    """Return the SHA-256 digest of a file."""

    digest = hashlib.sha256()
    with path.open("rb") as file:
        while True:
            check_stop()
            chunk = file.read(_COPY_CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _terminate_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    with suppress(subprocess.TimeoutExpired):
        process.wait(timeout=_PROCESS_KILL_TIMEOUT_SECONDS)
    if process.poll() is None:
        process.kill()
        process.wait()


def _process_error(command: str, stderr: BinaryIO) -> str:
    stderr.seek(0)
    detail = stderr.read().decode("utf-8", errors="replace").strip()
    if not detail:
        return f"{command} failed"
    return f"{command} failed: {detail}"


def _required_executable(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise SnapshotError(f"required executable not found: {name}")
    return path


def _release_metadata_path(owner: str, repo: str, tag: str | None) -> str:
    if tag:
        return f"repos/{owner}/{repo}/releases/tags/{quote(tag, safe='')}"
    return f"repos/{owner}/{repo}/releases/latest"
