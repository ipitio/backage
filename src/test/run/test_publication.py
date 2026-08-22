"""Tests for final source and index summary publication."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from bkg_py.database.composition import DatabaseRepositories
from bkg_py.database.dashboard import (
    DashboardDistributionItem,
    DashboardFreshnessBucket,
    DashboardMetricCoverage,
    DashboardProjection,
)
from bkg_py.database.models import (
    DatabaseRotationEvent,
    PackageInventory,
    PackageRecord,
    PackageRef,
)
from bkg_py.database.settings import DatabaseSettings
from bkg_py.database.support import DatabaseError
from bkg_py.publication.site_shell import SITE_SHELL_VERSION
from bkg_py.run.publication import (
    RunPublicationIdentity,
    RunPublicationPaths,
    RunPublicationRepositories,
    RunPublicationRequest,
    RunPublicationService,
)
from bkg_py.runtime_names import RunFile
from bkg_py.state import StateStore

_SITE_ENTRYPOINT = "index.html"


@dataclass(frozen=True)
class _InventoryRepository:
    inventory: PackageInventory
    dashboard_error: str | None = None

    def package_inventory(self) -> PackageInventory:
        """Return the fixed inventory used by a publication test."""

        return self.inventory

    def dashboard_projection(self, today: str) -> DashboardProjection:
        """Return matching bounded analytics or a configured read failure."""

        _ = today
        if self.dashboard_error is not None:
            raise DatabaseError(self.dashboard_error)
        return _dashboard_projection(self.inventory)


def _publication_repositories(
    repository: _InventoryRepository,
) -> RunPublicationRepositories:
    return RunPublicationRepositories(repository, repository)


def _dashboard_projection(inventory: PackageInventory) -> DashboardProjection:
    return DashboardProjection(
        inventory=inventory,
        resolved_packages=inventory.packages,
        package_types=(DashboardDistributionItem("container", inventory.packages),),
        other_packages=0,
        freshness=(
            DashboardFreshnessBucket("today", inventory.packages),
            DashboardFreshnessBucket("days_1_7", 0),
            DashboardFreshnessBucket("days_8_30", 0),
            DashboardFreshnessBucket("days_31_plus", 0),
            DashboardFreshnessBucket("unknown", 0),
        ),
        metrics=tuple(
            DashboardMetricCoverage(name, unit, 0, 0)
            for name, unit in (
                ("size", "bytes"),
                ("downloads_total", "downloads"),
                ("downloads_month", "downloads"),
                ("downloads_week", "downloads"),
                ("downloads_day", "downloads"),
            )
        ),
    )


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
    (images / "logo-b.webp").write_bytes(b"logo")
    (images / "logo.ico").write_bytes(b"icon")
    _write_site_shell(root / "site-shell")


def _write_site_shell(path: Path) -> None:
    content = b'dashboard <a href="__BKG_LATEST_RELEASE_URL__">release</a>\n'
    entrypoint = path / _SITE_ENTRYPOINT
    entrypoint.parent.mkdir(parents=True)
    entrypoint.write_bytes(content)
    (path / ".bkg-site-manifest.json").write_text(
        json.dumps(
            {
                "dashboard_schema_version": 1,
                "entrypoint": _SITE_ENTRYPOINT,
                "files": [
                    {
                        "bytes": len(content),
                        "path": _SITE_ENTRYPOINT,
                        "sha256": hashlib.sha256(content).hexdigest(),
                    }
                ],
                "schema_version": 1,
                "site_shell_version": SITE_SHELL_VERSION,
            }
        ),
        encoding="utf-8",
    )


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
    for name in RunFile:
        (working / name).write_text("compatibility", encoding="utf-8")
    state.set_many(
        {
            "BKG_PACKAGES_PENDING": "package",
            "BKG_OWNERS_QUEUE": "owner",
            "BKG_PAGE_2": "marker",
            "BKG_PAGE_ALL": "1",
            "BKG_INDEX_CLEANUP_DONE": "1",
            "BKG_TIMEOUT": "1",
            "UNKNOWN": "kept",
        }
    )
    inventory = PackageInventory(owners=12, repositories=345, packages=1200)
    messages: list[str] = []

    result = RunPublicationService(
        _publication_repositories(_InventoryRepository(inventory)),
        state,
        lambda: None,
        messages.append,
    ).publish(
        RunPublicationRequest(
            paths=RunPublicationPaths(
                root=root,
                index_directory=index,
                working_directory=working,
                site_shell_directory=root / "site-shell",
            ),
            identity=RunPublicationIdentity(
                github_owner="example",
                github_repo="backage",
                github_branch="master",
            ),
            today="2026-07-02",
            rotation_events=(
                DatabaseRotationEvent(
                    release_tag="v2026.7.0",
                    rotated_at="2026-07-01T03:04:05.000006Z",
                    archive_name=("2026.07.01T03.04.05.000006Z.index.db.zst"),
                    source_bytes=200,
                    compressed_bytes=75,
                    retained_since="2026-06-12",
                ),
            ),
        )
    )

    assert result == inventory
    assert (
        (root / "CHANGELOG.md")
        .read_text(encoding="utf-8")
        .startswith("2026-07-02 12 345 1200\nP.S. The database was rotated")
    )
    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "[v2026.7.0 release]" in changelog
    assert "`2026.07.01T03.04.05.000006Z.index.db.zst`" in changelog
    assert "source 200 bytes; compressed 75 bytes" in changelog
    source_readme = (root / "README.md").read_text(encoding="utf-8")
    assert source_readme.startswith("example/backage/master 2026-07-02 1200")
    index_readme = (index / "README.md").read_text(encoding="utf-8")
    assert "logo-b.webp" in index_readme
    assert "src/img/logo-b.webp" not in index_readme
    assert "```prolog" in index_readme
    assert "```jboss-cli" in index_readme
    assert (index / "logo-b.webp").read_bytes() == b"logo"
    assert (index / "favicon.ico").read_bytes() == b"icon"
    assert (index / "index.html").read_text(encoding="utf-8") == (
        'dashboard <a href="https://github.com/example/backage/releases/latest">'
        "release</a>\n"
    )
    assert not (index / "fxp.min.js").exists()

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
    dashboard = json.loads((index / "dashboard.json").read_text(encoding="utf-8"))
    assert dashboard["inventory"]["packages"] == 1200
    assert (
        json.loads((index / "dashboard-history.json").read_text(encoding="utf-8"))[
            "samples"
        ][0]["date"]
        == "2026-07-02"
    )
    assert messages[-2].startswith("Dashboard publication telemetry: ")
    assert messages[-1].startswith("Site shell publication telemetry: ")
    assert (sidecars / "keep.json").is_file()
    assert not any(path.name != "keep.json" for path in sidecars.iterdir())
    assert not any((working / name).exists() for name in RunFile)
    assert state.snapshot() == {
        "BKG_TIMEOUT": "1",
        "UNKNOWN": "kept",
    }


def test_run_publication_retains_dashboard_when_projection_fails(
    tmp_path: Path,
) -> None:
    """Optional analytics cannot block snapshot-compatible summary publication."""

    root = tmp_path / "repo"
    index = root / "index"
    _write_sources(root)
    index.mkdir()
    dashboard = index / "dashboard.json"
    history = index / "dashboard-history.json"
    dashboard.write_bytes(b"prior dashboard\n")
    history.write_bytes(b"prior history\n")
    messages: list[str] = []
    inventory = PackageInventory(1, 2, 3)

    result = RunPublicationService(
        _publication_repositories(_InventoryRepository(inventory, "projection failed")),
        StateStore(tmp_path / "state.env"),
        lambda: None,
        messages.append,
    ).publish(
        RunPublicationRequest(
            paths=RunPublicationPaths(
                root,
                index,
                tmp_path / "working",
                root / "site-shell",
            ),
            identity=RunPublicationIdentity("example", "backage", "master"),
            today="2026-07-02",
        )
    )

    assert result == inventory
    assert (
        json.loads((index / ".json").read_text(encoding="utf-8"))["raw_packages"] == 3
    )
    assert dashboard.read_bytes() == b"prior dashboard\n"
    assert history.read_bytes() == b"prior history\n"
    assert messages[0] == (
        "Dashboard projection unavailable; keeping previous artifacts: "
        "projection failed"
    )
    assert messages[1].startswith("Site shell publication telemetry: ")


def test_run_publication_retains_shell_when_bundle_verification_fails(
    tmp_path: Path,
) -> None:
    """Optional site-shell failure cannot block generated data publication."""

    root = tmp_path / "repo"
    index = root / "index"
    _write_sources(root)
    index.mkdir()
    prior_shell = index / _SITE_ENTRYPOINT
    prior_shell.write_bytes(b"prior shell\n")
    (root / "site-shell" / _SITE_ENTRYPOINT).write_bytes(b"corrupt shell\n")
    messages: list[str] = []

    result = RunPublicationService(
        _publication_repositories(_InventoryRepository(PackageInventory(1, 2, 3))),
        StateStore(tmp_path / "state.env"),
        lambda: None,
        messages.append,
    ).publish(
        RunPublicationRequest(
            paths=RunPublicationPaths(
                root,
                index,
                tmp_path / "working",
                root / "site-shell",
            ),
            identity=RunPublicationIdentity("example", "backage", "master"),
            today="2026-07-02",
        )
    )

    assert result == PackageInventory(1, 2, 3)
    assert (index / "dashboard.json").is_file()
    assert prior_shell.read_bytes() == b"prior shell\n"
    assert messages[-1].startswith(
        "Site shell publication unavailable; retaining current usable shell state: "
    )


def test_package_inventory_counts_distinct_published_paths(tmp_path: Path) -> None:
    """Inventory counts preserve the prior owner and repository grouping."""

    repository = DatabaseRepositories(DatabaseSettings(tmp_path / "index.db"))
    packages = (
        PackageRef("1", "users", "container", "Alpha", "one", "a"),
        PackageRef("1", "users", "container", "Alpha", "one", "b"),
        PackageRef("1", "users", "container", "Alpha", "two", "c"),
        PackageRef("2", "orgs", "container", "Beta", "one", "d"),
    )
    for package in packages:
        repository.packages.write_package(
            PackageRecord(package, 1, 1, 1, 1, 1, "2026-07-02")
        )

    inventory = repository.packages.package_inventory()

    assert inventory == PackageInventory(owners=2, repositories=3, packages=4)


def test_publication_rejects_rotation_events_from_another_release(
    tmp_path: Path,
) -> None:
    """A stale release event cannot leak into current release notes."""

    event = DatabaseRotationEvent(
        "v2026.6.1",
        "2026-06-28T00:00:00.000000Z",
        "2026.06.28T00.00.00.000000Z.index.db.zst",
        200,
        75,
        "2026-06-12",
    )
    request = RunPublicationRequest(
        paths=RunPublicationPaths(tmp_path, tmp_path / "index", tmp_path),
        identity=RunPublicationIdentity("owner", "repo", "master"),
        today="2026-07-02",
        rotation_events=(event,),
    )

    with pytest.raises(ValueError, match=r"do not belong to release v2026\.7\.0"):
        RunPublicationService(
            _publication_repositories(_InventoryRepository(PackageInventory(1, 1, 1))),
            StateStore(tmp_path / "state.env"),
            lambda: None,
        ).publish(request)
