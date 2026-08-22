"""Tests for verified static site-shell publication."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from bkg_py.publication.site_shell import (
    SITE_MANIFEST_FILE,
    SITE_SHELL_VERSION,
    GitHubRepositoryIdentity,
    SiteShellError,
    publish_site_shell,
)

_LATEST_RELEASE_TOKEN = b"__BKG_LATEST_RELEASE_URL__"
_LATEST_RELEASE_URL = b"https://github.com/example/backage/releases/latest"
_REPOSITORY = GitHubRepositoryIdentity("example", "backage")


def _write_shell(
    root: Path,
    files: dict[str, bytes],
    *,
    dashboard_schema_version: int = 1,
    entrypoint: str = "index.html",
    site_shell_version: int = SITE_SHELL_VERSION,
) -> bytes:
    entries: list[dict[str, object]] = []
    for path, content in sorted(files.items()):
        output = root / path
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(content)
        entries.append(
            {
                "bytes": len(content),
                "path": path,
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    manifest = (
        json.dumps(
            {
                "dashboard_schema_version": dashboard_schema_version,
                "entrypoint": entrypoint,
                "files": entries,
                "schema_version": 1,
                "site_shell_version": site_shell_version,
            },
            indent=2,
        )
        + "\n"
    ).encode()
    root.mkdir(parents=True, exist_ok=True)
    (root / SITE_MANIFEST_FILE).write_bytes(manifest)
    return manifest


def test_site_shell_publishes_verified_files_and_prunes_only_prior_ownership(
    tmp_path: Path,
) -> None:
    """A new manifest replaces its predecessor without sweeping index data."""

    source = tmp_path / "source"
    destination = tmp_path / "index"
    prior_index = b"prior candidate\n"
    _write_shell(
        destination,
        {
            ".bkg-site/candidate/index.html": prior_index,
            ".bkg-site/candidate/_astro/app.js": b"old javascript\n",
        },
        entrypoint=".bkg-site/candidate/index.html",
        site_shell_version=2,
    )
    (destination / "fxp.min.js").write_bytes(b"retired converter\n")
    package = destination / "owner/repository/package.json"
    package.parent.mkdir(parents=True)
    package.write_bytes(b"package data\n")
    unmanaged = destination / ".bkg-site/unmanaged.txt"
    unmanaged.write_bytes(b"unmanaged\n")
    _write_shell(
        source,
        {
            ".bkg-site/assets/app.css": b"new css\n",
            "index.html": b"new dashboard " + _LATEST_RELEASE_TOKEN + b"\n",
        },
    )

    result = publish_site_shell(
        source,
        destination,
        dashboard_schema_version=1,
        repository=_REPOSITORY,
        check_stop=lambda: None,
    )

    assert result.files == 2
    assert result.removed_files == 3
    assert result.entrypoint == "index.html"
    assert (destination / result.entrypoint).read_bytes() == (
        b"new dashboard " + _LATEST_RELEASE_URL + b"\n"
    )
    assert (destination / ".bkg-site/assets/app.css").read_bytes() == b"new css\n"
    assert not (destination / ".bkg-site/candidate/index.html").exists()
    assert not (destination / ".bkg-site/candidate/_astro/app.js").exists()
    assert not (destination / "fxp.min.js").exists()
    assert package.read_bytes() == b"package data\n"
    assert unmanaged.read_bytes() == b"unmanaged\n"
    manifest = json.loads(
        (destination / SITE_MANIFEST_FILE).read_text(encoding="utf-8")
    )
    published_entrypoint = (destination / result.entrypoint).read_bytes()
    entry = next(
        item for item in manifest["files"] if item["path"] == result.entrypoint
    )
    assert entry["bytes"] == len(published_entrypoint)
    assert entry["sha256"] == hashlib.sha256(published_entrypoint).hexdigest()


@pytest.mark.parametrize("path", ["../README.md", "owner/repository/package.json"])
def test_site_shell_rejects_paths_outside_its_namespace(
    tmp_path: Path,
    path: str,
) -> None:
    """A generated manifest cannot claim source or package-index files."""

    source = tmp_path / "source"
    source.mkdir()
    content = b"unsafe\n"
    manifest = {
        "dashboard_schema_version": 1,
        "entrypoint": path,
        "files": [
            {
                "bytes": len(content),
                "path": path,
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        ],
        "schema_version": 1,
        "site_shell_version": SITE_SHELL_VERSION,
    }
    (source / SITE_MANIFEST_FILE).write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(SiteShellError, match="not shell-owned"):
        publish_site_shell(
            source,
            tmp_path / "index",
            dashboard_schema_version=1,
            repository=_REPOSITORY,
            check_stop=lambda: None,
        )


def test_site_shell_verifies_all_content_before_replacing_entrypoint(
    tmp_path: Path,
) -> None:
    """A corrupt bundled asset leaves the previously published shell intact."""

    source = tmp_path / "source"
    destination = tmp_path / "index"
    entrypoint = "index.html"
    _write_shell(source, {entrypoint: b"new " + _LATEST_RELEASE_TOKEN + b"\n"})
    (source / entrypoint).write_bytes(b"corrupt\n")
    prior_manifest = _write_shell(destination, {entrypoint: b"prior candidate\n"})
    retired = destination / "fxp.min.js"
    retired.write_bytes(b"still in use\n")

    with pytest.raises(SiteShellError, match="size mismatch"):
        publish_site_shell(
            source,
            destination,
            dashboard_schema_version=1,
            repository=_REPOSITORY,
            check_stop=lambda: None,
        )

    assert (destination / entrypoint).read_bytes() == b"prior candidate\n"
    assert (destination / SITE_MANIFEST_FILE).read_bytes() == prior_manifest
    assert retired.read_bytes() == b"still in use\n"


def test_site_shell_checks_for_stop_before_writing(tmp_path: Path) -> None:
    """A graceful stop leaves the prior entrypoint and manifest unchanged."""

    source = tmp_path / "source"
    destination = tmp_path / "index"
    entrypoint = "index.html"
    _write_shell(source, {entrypoint: b"new " + _LATEST_RELEASE_TOKEN + b"\n"})
    prior_manifest = _write_shell(destination, {entrypoint: b"prior candidate\n"})

    def stop() -> None:
        raise RuntimeError("stop requested")

    with pytest.raises(RuntimeError, match="stop requested"):
        publish_site_shell(
            source,
            destination,
            dashboard_schema_version=1,
            repository=_REPOSITORY,
            check_stop=stop,
        )

    assert (destination / entrypoint).read_bytes() == b"prior candidate\n"
    assert (destination / SITE_MANIFEST_FILE).read_bytes() == prior_manifest


def test_site_shell_keeps_prior_files_when_its_manifest_is_invalid(
    tmp_path: Path,
) -> None:
    """Unknown prior ownership prevents cleanup and manifest replacement."""

    source = tmp_path / "source"
    destination = tmp_path / "index"
    entrypoint = "index.html"
    _write_shell(source, {entrypoint: b"new " + _LATEST_RELEASE_TOKEN + b"\n"})
    old_entrypoint = destination / entrypoint
    old_entrypoint.parent.mkdir(parents=True)
    old_entrypoint.write_bytes(b"prior candidate\n")
    (destination / SITE_MANIFEST_FILE).write_bytes(b"not json\n")

    with pytest.raises(SiteShellError, match="not valid JSON"):
        publish_site_shell(
            source,
            destination,
            dashboard_schema_version=1,
            repository=_REPOSITORY,
            check_stop=lambda: None,
        )

    assert old_entrypoint.read_bytes() == b"prior candidate\n"
    assert (destination / SITE_MANIFEST_FILE).read_bytes() == b"not json\n"


def test_site_shell_rejects_symbolic_link_destination_parents(
    tmp_path: Path,
) -> None:
    """A manifest cannot write through a linked directory outside the index."""

    source = tmp_path / "source"
    destination = tmp_path / "index"
    outside = tmp_path / "outside"
    entrypoint = "index.html"
    _write_shell(
        source,
        {
            entrypoint: b"new " + _LATEST_RELEASE_TOKEN + b"\n",
            ".bkg-site/assets/app.js": b"javascript\n",
        },
    )
    destination.mkdir()
    outside.mkdir()
    (destination / ".bkg-site").symlink_to(outside, target_is_directory=True)

    with pytest.raises(SiteShellError, match="symbolic-link parent"):
        publish_site_shell(
            source,
            destination,
            dashboard_schema_version=1,
            repository=_REPOSITORY,
            check_stop=lambda: None,
        )

    assert not (outside / "assets/app.js").exists()
