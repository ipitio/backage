"""Tests for final source and index summary publication."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from bkg_py.database import DatabaseRepository, DatabaseSettings
from bkg_py.database_models import PackageInventory, PackageRecord, PackageRef
from bkg_py.run_publication import (
    RunPublicationIdentity,
    RunPublicationPaths,
    RunPublicationRequest,
    RunPublicationService,
)
from bkg_py.state import StateStore


@dataclass(frozen=True)
class _InventoryRepository:
    inventory: PackageInventory

    def package_inventory(self) -> PackageInventory:
        """Return the fixed inventory used by a publication test."""

        return self.inventory


def _write_sources(root: Path) -> None:
    templates = root / "src" / "templates"
    images = root / "src" / "img"
    templates.mkdir(parents=True)
    images.mkdir(parents=True)
    (templates / ".CHANGELOG.md").write_text(
        "[DATE] [OWNERS] [REPOS] [PACKAGES]\n",
        encoding="utf-8",
    )
    (templates / ".README.md").write_text(
        "<GITHUB_OWNER>/<GITHUB_REPO>/<GITHUB_BRANCH> [DATE] [PACKAGES]\n"
        "src/img/logo-b.webp\n```py\n```js\n",
        encoding="utf-8",
    )
    (templates / ".index.html").write_text(
        "<title>GITHUB_REPO</title>\n",
        encoding="utf-8",
    )
    (templates / "fxp.min.js").write_bytes(b"javascript")
    (images / "logo-b.webp").write_bytes(b"logo")
    (images / "logo.ico").write_bytes(b"icon")


def test_run_publication_hydrates_outputs_and_prunes_transient_state(
    tmp_path: Path,
) -> None:
    """Final publication replaces summaries and removes only transient data."""

    root = tmp_path / "repo"
    index = root / "index"
    working = tmp_path / "working"
    state = StateStore(tmp_path / "state.env")
    _write_sources(root)
    index.mkdir()
    working.mkdir()
    sidecars = index / "owner" / "repo"
    sidecars.mkdir(parents=True)
    for name in ("a.json.tmp", "b.json.abs.2", "c.json.rel.worker"):
        (sidecars / name).write_text("temporary", encoding="utf-8")
    (sidecars / "keep.json").write_text("published", encoding="utf-8")
    for name in ("packages_all", "packages_to_update", "packages_already_updated"):
        (working / name).write_text("compatibility", encoding="utf-8")
    state.set_many(
        {
            "BKG_PACKAGES_PENDING": "package",
            "BKG_OWNERS_QUEUE": "owner",
            "BKG_PAGE_2": "marker",
            "BKG_PAGE_ALL": "1",
            "BKG_TIMEOUT": "1",
            "UNKNOWN": "kept",
        }
    )
    inventory = PackageInventory(owners=12, repositories=345, packages=1200)

    result = RunPublicationService(
        _InventoryRepository(inventory),
        state,
        lambda: None,
    ).publish(
        RunPublicationRequest(
            paths=RunPublicationPaths(
                root=root,
                index_directory=index,
                working_directory=working,
            ),
            identity=RunPublicationIdentity(
                github_owner="example",
                github_repo="backage",
                github_branch="master",
            ),
            today="2026-07-02",
            rotated=True,
        )
    )

    assert result == inventory
    assert (
        (root / "CHANGELOG.md")
        .read_text(encoding="utf-8")
        .startswith("2026-07-02 12 345 1200\nP.S. The database was rotated")
    )
    source_readme = (root / "README.md").read_text(encoding="utf-8")
    assert source_readme.startswith("example/backage/master 2026-07-02 1200")
    index_readme = (index / "README.md").read_text(encoding="utf-8")
    assert "logo-b.webp" in index_readme
    assert "src/img/logo-b.webp" not in index_readme
    assert "```prolog" in index_readme
    assert "```jboss-cli" in index_readme
    assert (index / "logo-b.webp").read_bytes() == b"logo"
    assert (index / "favicon.ico").read_bytes() == b"icon"
    assert (index / "fxp.min.js").read_bytes() == b"javascript"
    assert (index / "index.html").read_text(encoding="utf-8") == (
        "<title>backage</title>\n"
    )

    summary = json.loads((index / ".json").read_text(encoding="utf-8"))
    assert summary == {
        "owners": "12",
        "repos": "345",
        "packages": "1.2k",
        "raw_owners": 12,
        "raw_repos": 345,
        "raw_packages": 1200,
        "date": "2026-07-02",
    }
    assert "<raw_packages>1200</raw_packages>" in (index / ".xml").read_text(
        encoding="utf-8"
    )
    assert (sidecars / "keep.json").is_file()
    assert not any(path.name != "keep.json" for path in sidecars.iterdir())
    assert not any(
        (working / name).exists()
        for name in (
            "packages_all",
            "packages_to_update",
            "packages_already_updated",
        )
    )
    assert state.snapshot() == {
        "BKG_PAGE_ALL": "1",
        "BKG_TIMEOUT": "1",
        "UNKNOWN": "kept",
    }


def test_package_inventory_counts_distinct_published_paths(tmp_path: Path) -> None:
    """Inventory counts preserve the prior owner and repository grouping."""

    repository = DatabaseRepository(DatabaseSettings(tmp_path / "index.db"))
    packages = (
        PackageRef("1", "users", "container", "Alpha", "one", "a"),
        PackageRef("1", "users", "container", "Alpha", "one", "b"),
        PackageRef("1", "users", "container", "Alpha", "two", "c"),
        PackageRef("2", "orgs", "container", "Beta", "one", "d"),
    )
    for package in packages:
        repository.write_package(PackageRecord(package, 1, 1, 1, 1, 1, "2026-07-02"))

    inventory = repository.package_inventory()

    assert inventory == PackageInventory(owners=2, repositories=3, packages=4)
