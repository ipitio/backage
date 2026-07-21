"""Integration tests for the complete Python run command."""

from __future__ import annotations

from pathlib import Path

import pytest

from bkg_py.cli import main
from bkg_py.result import ExitStatus
from bkg_py.state import StateStore


def test_clean_mode_runs_startup_and_publication_without_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mode two publishes empty summaries without GitHub or snapshot work."""

    root = tmp_path / "root"
    working_directory = root / "work"
    index_directory = root / "index"
    state_path = root / "src/env.env"
    database_path = root / "index.db"
    _write_publication_sources(root)
    working_directory.mkdir(parents=True)
    (root / "owners.txt").write_text("", encoding="utf-8")
    (root / "optout.txt").write_text("", encoding="utf-8")

    monkeypatch.setenv("BKG_ROOT", str(root))
    monkeypatch.setenv("BKG_ENV", str(state_path))
    monkeypatch.setenv("BKG_INDEX", "index")
    monkeypatch.setenv("BKG_INDEX_DB", str(database_path))
    monkeypatch.setenv("BKG_INDEX_DIR", str(index_directory))
    monkeypatch.setenv("GITHUB_BRANCH", "master")
    monkeypatch.setenv("GITHUB_OWNER", "example")
    monkeypatch.setenv("GITHUB_REPO", "backage")

    status = main(
        [
            "run",
            "--mode",
            "2",
            "--working-directory",
            str(working_directory),
        ]
    )

    assert status == ExitStatus.SUCCESS
    assert database_path.stat().st_size > 0
    assert (root / "README.md").is_file()
    assert (root / "CHANGELOG.md").is_file()
    assert (index_directory / "README.md").is_file()
    assert (index_directory / ".json").is_file()
    assert (index_directory / ".xml").is_file()
    assert not (root / ".snapshot").exists()
    assert StateStore(state_path).get("BKG_TIMEOUT") is None


def _write_publication_sources(root: Path) -> None:
    templates = root / "src/templates"
    images = root / "src/img"
    templates.mkdir(parents=True)
    images.mkdir(parents=True)
    (templates / ".CHANGELOG.md").write_text(
        "[DATE] [OWNERS] [REPOS] [PACKAGES]\n",
        encoding="utf-8",
    )
    (templates / ".README.md").write_text(
        "<GITHUB_OWNER>/<GITHUB_REPO>@<GITHUB_BRANCH> "
        "[DATE] [OWNERS] [REPOS] [PACKAGES]\n",
        encoding="utf-8",
    )
    (templates / ".index.html").write_text(
        "<title>GITHUB_REPO</title>\n",
        encoding="utf-8",
    )
    (templates / "fxp.min.js").write_bytes(b"javascript")
    (images / "logo-b.webp").write_bytes(b"logo")
    (images / "logo.ico").write_bytes(b"icon")
