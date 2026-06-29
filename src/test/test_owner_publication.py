"""Tests for owner and repository aggregate publication."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bkg_py.database import DatabaseRepository
from bkg_py.database_models import (
    PackageRecord,
    PackageRef,
    VersionMetrics,
    VersionRecord,
    VersionStage,
)
from bkg_py.database_settings import DatabaseSettings
from bkg_py.owner_publication import (
    OwnerPublicationRequest,
    OwnerPublicationService,
)
from bkg_py.publication import PublicationLimits
from bkg_py.rendering import AggregateSettings
from bkg_py.runtime import GracefulStop

_TODAY = "2026-06-28"


def _package(repo: str, name: str) -> PackageRef:
    return PackageRef(
        owner_id="42",
        owner_type="orgs",
        package_type="container",
        owner="Example",
        repo=repo,
        package=name,
    )


def _write_package(repository: DatabaseRepository, package: PackageRef) -> None:
    repository.write_package(
        PackageRecord(
            package,
            downloads=100,
            downloads_month=20,
            downloads_week=10,
            downloads_day=2,
            size=30,
            date=_TODAY,
        )
    )
    repository.flush_version_stage(
        VersionStage(
            package,
            f"versions_{package.repo}_{package.package}",
            False,
            (
                VersionRecord(
                    "1",
                    "sha256:one",
                    VersionMetrics(30, 100, 20, 10, 2),
                    _TODAY,
                    "latest",
                ),
            ),
        )
    )


def _service(
    repository: DatabaseRepository,
    check_stop: object = None,
) -> OwnerPublicationService:
    stop = check_stop if callable(check_stop) else lambda: None
    return OwnerPublicationService(
        repository,
        AggregateSettings(target_bytes=1_000_000),
        PublicationLimits(maximum_bytes=1_000_000, hard_maximum_bytes=2_000_000),
        stop,
    )


def test_owner_publication_writes_database_backed_json_xml_pairs(
    tmp_path: Path,
) -> None:
    """Owner and repository endpoints are published from current rows."""

    repository = DatabaseRepository(DatabaseSettings(tmp_path / "index.db"))
    _write_package(repository, _package("One", "alpha"))
    _write_package(repository, _package("Two", "beta"))

    result = _service(repository).publish(
        OwnerPublicationRequest("42", "Example", tmp_path / "index")
    )

    owner_directory = tmp_path / "index" / "Example"
    owner_packages = json.loads((owner_directory / ".json").read_bytes())
    assert result.package_count == 2
    assert result.repositories == ("One", "Two")
    assert [package["package"] for package in owner_packages] == ["alpha", "beta"]
    assert (owner_directory / ".xml").read_text(encoding="utf-8").count(
        "<owner_type>"
    ) == 2
    assert (
        json.loads((owner_directory / "One" / ".json").read_bytes())[0]["package"]
        == "alpha"
    )
    assert (owner_directory / "One" / ".xml").is_file()
    assert (owner_directory / "Two" / ".xml").is_file()


def test_interrupted_owner_publication_preserves_existing_pair(tmp_path: Path) -> None:
    """A stop during temporary rendering cannot split the published pair."""

    repository = DatabaseRepository(DatabaseSettings(tmp_path / "index.db"))
    _write_package(repository, _package("One", "alpha"))
    owner_directory = tmp_path / "index" / "Example"
    owner_directory.mkdir(parents=True)
    json_path = owner_directory / ".json"
    xml_path = owner_directory / ".xml"
    json_path.write_text('{"old":true}\n', encoding="utf-8")
    xml_path.write_text("<old/>\n", encoding="utf-8")

    def stop() -> None:
        raise GracefulStop("test")

    with pytest.raises(GracefulStop, match="test"):
        _service(repository, stop).publish(
            OwnerPublicationRequest("42", "Example", tmp_path / "index")
        )

    assert json_path.read_text(encoding="utf-8") == '{"old":true}\n'
    assert xml_path.read_text(encoding="utf-8") == "<old/>\n"
    assert not tuple(owner_directory.glob("..json.render.*"))


def test_empty_owner_publication_removes_stale_aggregate_pair(tmp_path: Path) -> None:
    """An owner without database packages cannot retain aggregate endpoints."""

    repository = DatabaseRepository(DatabaseSettings(tmp_path / "index.db"))
    repository.ensure_schema()
    owner_directory = tmp_path / "index" / "Example"
    owner_directory.mkdir(parents=True)
    (owner_directory / ".json").write_text("[]\n", encoding="utf-8")
    (owner_directory / ".xml").write_text("<xml/>\n", encoding="utf-8")

    result = _service(repository).publish(
        OwnerPublicationRequest("42", "Example", tmp_path / "index")
    )

    assert result.package_count == 0
    assert result.repositories == ()
    assert not owner_directory.exists()
