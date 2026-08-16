"""Verify and publish the static Pages shell built with Astro."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import cast

from .files import atomic_binary_output

SITE_MANIFEST_FILE = ".bkg-site-manifest.json"
SITE_MANIFEST_SCHEMA_VERSION = 1
SITE_SHELL_VERSION = 3
SITE_CONTENT_DIRECTORY = ".bkg-site"
_SITE_RESOURCE_PARTS = ("share", "backage", "site")
_MAX_MANIFEST_BYTES = 1_000_000
_MAX_SITE_FILES = 512
_MAX_SITE_BYTES = 16_000_000
_SHA256 = re.compile(r"[0-9a-f]{64}")
_GITHUB_OWNER = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")
_GITHUB_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]{1,100}")
_LATEST_RELEASE_TOKEN = b"__BKG_LATEST_RELEASE_URL__"
_RETIRED_SITE_FILES = ("fxp.min.js",)


class SiteShellError(RuntimeError):
    """A built site shell cannot be verified or published safely."""


@dataclass(frozen=True)
class SiteShellPublicationResult:
    """Summary of one completed static-shell publication."""

    bytes: int
    entrypoint: str
    files: int
    removed_files: int
    site_shell_version: int


@dataclass(frozen=True)
class GitHubRepositoryIdentity:
    """GitHub owner and repository used to hydrate shell navigation."""

    owner: str
    repository: str


@dataclass(frozen=True)
class _ManifestFile:
    path: str
    bytes: int
    sha256: str


@dataclass(frozen=True)
class _SiteManifest:
    dashboard_schema_version: int
    entrypoint: str
    files: tuple[_ManifestFile, ...]
    schema_version: int
    site_shell_version: int


def default_site_shell_directory() -> Path:
    """Return the read-only shell directory installed beside the application."""

    return Path(sys.prefix).joinpath(*_SITE_RESOURCE_PARTS)


def publish_site_shell(
    source_directory: Path,
    destination_directory: Path,
    *,
    dashboard_schema_version: int,
    repository: GitHubRepositoryIdentity,
    check_stop: Callable[[], None],
) -> SiteShellPublicationResult:
    """Verify one built shell and atomically publish only its declared files."""

    source_manifest_path = source_directory / SITE_MANIFEST_FILE
    manifest, manifest_content = _read_manifest(
        source_manifest_path,
        label="bundled",
    )
    if manifest.schema_version != SITE_MANIFEST_SCHEMA_VERSION:
        raise SiteShellError(
            "bundled site manifest uses unsupported schema version "
            f"{manifest.schema_version}"
        )
    if manifest.site_shell_version != SITE_SHELL_VERSION:
        raise SiteShellError(
            f"bundled site shell uses unsupported version {manifest.site_shell_version}"
        )
    if manifest.dashboard_schema_version != dashboard_schema_version:
        raise SiteShellError(
            "bundled site shell expects dashboard schema version "
            f"{manifest.dashboard_schema_version}, not {dashboard_schema_version}"
        )
    content_by_path = _verified_content(source_directory, manifest)
    manifest, content_by_path = _hydrate_repository_links(
        manifest,
        content_by_path,
        repository,
    )
    manifest_content = _manifest_content(manifest)

    destination_directory.mkdir(parents=True, exist_ok=True)
    previous = _read_previous_manifest(destination_directory)
    _validate_destination_paths(destination_directory, manifest, previous)
    check_stop()
    entrypoint = manifest.entrypoint
    for item in manifest.files:
        if item.path == entrypoint:
            continue
        check_stop()
        _write_file(destination_directory / item.path, content_by_path[item.path])
    check_stop()
    _write_file(
        destination_directory / entrypoint,
        content_by_path[entrypoint],
    )
    removed_files = _remove_stale_files(
        destination_directory,
        previous,
        manifest,
    )
    removed_files += _remove_retired_files(destination_directory)
    _write_file(destination_directory / SITE_MANIFEST_FILE, manifest_content)
    return SiteShellPublicationResult(
        bytes=sum(item.bytes for item in manifest.files),
        entrypoint=entrypoint,
        files=len(manifest.files),
        removed_files=removed_files,
        site_shell_version=manifest.site_shell_version,
    )


def _hydrate_repository_links(
    manifest: _SiteManifest,
    content_by_path: dict[str, bytes],
    repository: GitHubRepositoryIdentity,
) -> tuple[_SiteManifest, dict[str, bytes]]:
    if _GITHUB_OWNER.fullmatch(repository.owner) is None:
        raise SiteShellError("GitHub owner cannot be used in the site shell")
    if _GITHUB_REPOSITORY.fullmatch(repository.repository) is None:
        raise SiteShellError("GitHub repository cannot be used in the site shell")
    if repository.repository in {".", ".."}:
        raise SiteShellError("GitHub repository cannot be used in the site shell")
    release_url = (
        f"https://github.com/{repository.owner}/{repository.repository}/releases/latest"
    ).encode()
    entrypoint = content_by_path[manifest.entrypoint]
    if entrypoint.count(_LATEST_RELEASE_TOKEN) != 1:
        raise SiteShellError(
            "bundled site shell must contain one latest-release link token"
        )
    hydrated_content = {
        path: content.replace(_LATEST_RELEASE_TOKEN, release_url)
        for path, content in content_by_path.items()
    }
    hydrated_files = tuple(
        _ManifestFile(
            path=item.path,
            bytes=len(hydrated_content[item.path]),
            sha256=hashlib.sha256(hydrated_content[item.path]).hexdigest(),
        )
        for item in manifest.files
    )
    return (
        _SiteManifest(
            dashboard_schema_version=manifest.dashboard_schema_version,
            entrypoint=manifest.entrypoint,
            files=hydrated_files,
            schema_version=manifest.schema_version,
            site_shell_version=manifest.site_shell_version,
        ),
        hydrated_content,
    )


def _manifest_content(manifest: _SiteManifest) -> bytes:
    value = {
        "dashboard_schema_version": manifest.dashboard_schema_version,
        "entrypoint": manifest.entrypoint,
        "files": [
            {
                "bytes": item.bytes,
                "path": item.path,
                "sha256": item.sha256,
            }
            for item in manifest.files
        ],
        "schema_version": manifest.schema_version,
        "site_shell_version": manifest.site_shell_version,
    }
    return (json.dumps(value, indent=2) + "\n").encode()


def _read_previous_manifest(destination: Path) -> _SiteManifest | None:
    path = destination / SITE_MANIFEST_FILE
    try:
        manifest, _content = _read_manifest(path, label="published")
    except FileNotFoundError:
        return None
    if manifest.schema_version != SITE_MANIFEST_SCHEMA_VERSION:
        raise SiteShellError(
            "published site manifest uses unsupported schema version "
            f"{manifest.schema_version}"
        )
    return manifest


def _read_manifest(path: Path, *, label: str) -> tuple[_SiteManifest, bytes]:
    size = path.stat().st_size
    if size > _MAX_MANIFEST_BYTES:
        raise SiteShellError(f"{label} site manifest exceeds the size limit")
    try:
        content = path.read_bytes()
        value: object = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SiteShellError(f"{label} site manifest is not valid JSON") from error
    return _parse_manifest(value, label=label), content


def _parse_manifest(value: object, *, label: str) -> _SiteManifest:
    document = _object_mapping(value)
    expected_fields = frozenset(
        {
            "dashboard_schema_version",
            "entrypoint",
            "files",
            "schema_version",
            "site_shell_version",
        }
    )
    if document is None or frozenset(document) != expected_fields:
        raise SiteShellError(f"{label} site manifest has unexpected fields")
    raw_files = document.get("files")
    if not isinstance(raw_files, list):
        raise SiteShellError(f"{label} site manifest has an invalid file list")
    file_values = cast(list[object], raw_files)
    if not 0 < len(file_values) <= _MAX_SITE_FILES:
        raise SiteShellError(f"{label} site manifest has an invalid file list")
    files = tuple(_parse_manifest_file(item, label=label) for item in file_values)
    paths = tuple(item.path for item in files)
    if len(paths) != len(set(paths)):
        raise SiteShellError(f"{label} site manifest contains duplicate paths")
    total_bytes = sum(item.bytes for item in files)
    if total_bytes > _MAX_SITE_BYTES:
        raise SiteShellError(f"{label} site shell exceeds the size limit")
    entrypoint = document.get("entrypoint")
    if not isinstance(entrypoint, str) or entrypoint not in paths:
        raise SiteShellError(f"{label} site manifest has an invalid entrypoint")
    _validate_owned_path(entrypoint, label=label)
    if PurePosixPath(entrypoint).name != "index.html":
        raise SiteShellError(f"{label} site entrypoint must be an index.html file")
    return _SiteManifest(
        dashboard_schema_version=_manifest_integer(
            document,
            "dashboard_schema_version",
            label,
        ),
        entrypoint=entrypoint,
        files=files,
        schema_version=_manifest_integer(document, "schema_version", label),
        site_shell_version=_manifest_integer(
            document,
            "site_shell_version",
            label,
        ),
    )


def _parse_manifest_file(value: object, *, label: str) -> _ManifestFile:
    document = _object_mapping(value)
    if document is None or frozenset(document) != frozenset(
        {"bytes", "path", "sha256"}
    ):
        raise SiteShellError(f"{label} site manifest has an invalid file entry")
    path = document.get("path")
    digest = document.get("sha256")
    if not isinstance(path, str):
        raise SiteShellError(f"{label} site manifest has a non-string path")
    _validate_owned_path(path, label=label)
    if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
        raise SiteShellError(f"{label} site manifest has an invalid SHA-256")
    return _ManifestFile(
        path=path,
        bytes=_manifest_integer(document, "bytes", label, allow_zero=True),
        sha256=digest,
    )


def _validate_owned_path(value: str, *, label: str) -> None:
    path = PurePosixPath(value)
    if (
        not value
        or value.startswith("/")
        or "\\" in value
        or path.as_posix() != value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise SiteShellError(f"{label} site manifest path is not shell-owned: {value}")
    if value != "index.html" and not value.startswith(f"{SITE_CONTENT_DIRECTORY}/"):
        raise SiteShellError(f"{label} site manifest path is not shell-owned: {value}")


def _manifest_integer(
    document: dict[str, object],
    name: str,
    label: str,
    *,
    allow_zero: bool = False,
) -> int:
    value = document.get(name)
    minimum = 0 if allow_zero else 1
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise SiteShellError(f"{label} site manifest has an invalid {name}")
    return value


def _verified_content(
    source_directory: Path,
    manifest: _SiteManifest,
) -> dict[str, bytes]:
    try:
        source_root = source_directory.resolve(strict=True)
    except FileNotFoundError as error:
        raise SiteShellError("bundled site shell directory is missing") from error
    content_by_path: dict[str, bytes] = {}
    for item in manifest.files:
        source = source_directory.joinpath(*PurePosixPath(item.path).parts)
        try:
            resolved = source.resolve(strict=True)
        except FileNotFoundError as error:
            raise SiteShellError(
                f"bundled site shell file is missing: {item.path}"
            ) from error
        if not resolved.is_relative_to(source_root) or _has_symlink(
            source_directory,
            PurePosixPath(item.path),
        ):
            raise SiteShellError(
                f"bundled site shell file is not a regular owned path: {item.path}"
            )
        try:
            content = source.read_bytes()
        except IsADirectoryError as error:
            raise SiteShellError(
                f"bundled site shell file is not regular: {item.path}"
            ) from error
        if len(content) != item.bytes:
            raise SiteShellError(f"bundled site shell size mismatch: {item.path}")
        if hashlib.sha256(content).hexdigest() != item.sha256:
            raise SiteShellError(f"bundled site shell hash mismatch: {item.path}")
        content_by_path[item.path] = content
    return content_by_path


def _has_symlink(root: Path, relative_path: PurePosixPath) -> bool:
    current = root
    for part in relative_path.parts:
        current /= part
        if current.is_symlink():
            return True
    return False


def _validate_destination_paths(
    destination: Path,
    current: _SiteManifest,
    previous: _SiteManifest | None,
) -> None:
    if destination.is_symlink():
        raise SiteShellError("site-shell destination is a symbolic link")
    paths = {item.path for item in current.files}
    if previous is not None:
        paths.update(item.path for item in previous.files)
    for value in paths:
        directory = destination
        for part in PurePosixPath(value).parts[:-1]:
            directory /= part
            if directory.is_symlink():
                raise SiteShellError(
                    f"site-shell destination has a symbolic-link parent: {value}"
                )


def _remove_stale_files(
    destination: Path,
    previous: _SiteManifest | None,
    current: _SiteManifest,
) -> int:
    if previous is None:
        return 0
    current_paths = {item.path for item in current.files}
    stale = sorted(
        (item.path for item in previous.files if item.path not in current_paths),
        reverse=True,
    )
    removed = 0
    for value in stale:
        path = destination.joinpath(*PurePosixPath(value).parts)
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        removed += 1
        _remove_empty_shell_directories(path.parent, destination)
    return removed


def _remove_retired_files(destination: Path) -> int:
    removed = 0
    for name in _RETIRED_SITE_FILES:
        path = destination / name
        if path.is_dir() and not path.is_symlink():
            continue
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        removed += 1
    return removed


def _remove_empty_shell_directories(directory: Path, root: Path) -> None:
    reserved = root / ".bkg-site"
    while directory != root and directory.is_relative_to(reserved):
        try:
            directory.rmdir()
        except OSError:
            return
        directory = directory.parent


def _write_file(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with atomic_binary_output(path) as output:
        output.write(content)


def _object_mapping(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    mapping = cast(dict[object, object], value)
    if not all(isinstance(key, str) for key in mapping):
        return None
    return cast(dict[str, object], mapping)

# Fix for issue #45: safe input handling
