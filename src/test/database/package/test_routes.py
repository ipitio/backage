"""Tests for lazy package routes, identity isolation, and metadata retirement."""

import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from bkg_py.database.composition import DatabaseRepositories
from bkg_py.database.models import (
    OwnerScanFailure,
    OwnerScanPackage,
    PackageCatalogPath,
    PackageRecord,
    PackageRef,
)
from bkg_py.database.settings import DatabaseSettings

_PACKAGE = PackageRef("42", "orgs", "maven", "Example", "repo", "org.example.library")
_DATE = "2026-09-01"


def _observe(repository: DatabaseRepositories, package: PackageRef) -> None:
    repository.owners.begin_owner_scan(package.owner_id, package.owner, "scan", 100)
    repository.owners.observe_owner_scan(
        package.owner_id,
        "scan",
        (
            OwnerScanPackage(
                package.owner_type,
                package.package_type,
                package.repo,
                package.package,
                source_package_id="12345",
            ),
        ),
        101,
    )


def test_route_is_populated_only_by_discovery_and_survives_restart(
    tmp_path: Path,
) -> None:
    """Creating the schema or writing historical metrics does not invent a route."""

    settings = DatabaseSettings(tmp_path / "index.db")
    repository = DatabaseRepositories(settings)
    repository.packages.write_package(PackageRecord(_PACKAGE, 1, 1, 1, 1, 1, _DATE))
    assert repository.packages.package_source_id(_PACKAGE) == ""
    with sqlite3.connect(settings.path) as connection:
        assert connection.execute(
            "select count(*) from bkg_package_routes"
        ).fetchone() == (0,)
    _observe(repository, _PACKAGE)

    resumed = DatabaseRepositories(settings)
    assert resumed.packages.package_source_id(_PACKAGE) == "12345"
    for changed in (
        replace(_PACKAGE, owner_id="43"),
        replace(_PACKAGE, owner="Other"),
        replace(_PACKAGE, repo="other"),
        replace(_PACKAGE, package="other"),
        replace(_PACKAGE, package_type="container"),
    ):
        assert resumed.packages.package_source_id(changed) == ""


@pytest.mark.parametrize("retirement", ["package", "owner", "alias", "inventory"])
def test_routes_are_removed_with_authoritative_retirement(
    tmp_path: Path, retirement: str
) -> None:
    """Opt-outs, verified absence, and owner renames do not leave old routes."""

    repository = DatabaseRepositories(DatabaseSettings(tmp_path / "index.db"))
    repository.packages.write_package(PackageRecord(_PACKAGE, 1, 1, 1, 1, 1, _DATE))
    _observe(repository, _PACKAGE)
    if retirement == "package":
        repository.packages.retire_package(_PACKAGE)
    elif retirement == "owner":
        repository.packages.retire_owner(_PACKAGE.owner)
    elif retirement == "alias":
        repository.owner_identities.retire_owner_aliases(_PACKAGE.owner_id, "Renamed")
    else:
        repository.owners.begin_owner_scan(
            _PACKAGE.owner_id, _PACKAGE.owner, "empty", 102
        )
        repository.owners.complete_owner_scan(_PACKAGE.owner_id, "empty", _DATE, 103)

    assert repository.packages.package_source_id(_PACKAGE) == ""


def test_rotation_keeps_live_routes_and_removes_abandoned_routes(
    tmp_path: Path,
) -> None:
    """History expiration does not break routing, but abandoned state is pruned."""

    repository = DatabaseRepositories(DatabaseSettings(tmp_path / "index.db"))
    abandoned = replace(_PACKAGE, owner_id="43", owner="Abandoned")
    repository.packages.write_package(PackageRecord(_PACKAGE, 1, 1, 1, 1, 1, _DATE))
    repository.catalog.initialize_package_catalog(
        (PackageCatalogPath(_PACKAGE.owner, _PACKAGE.repo, _PACKAGE.package),),
        "a" * 40,
        _DATE,
    )
    for package in (_PACKAGE, abandoned):
        _observe(repository, package)
    repository.owners.fail_owner_scan(
        OwnerScanFailure(
            abandoned.owner_id, abandoned.owner, "scan", "unavailable", 102
        )
    )

    repository.packages.cleanup_replaced_legacy_tables(
        since="2026-10-01", prune_normalized=True
    )

    assert repository.packages.package_snapshot(_PACKAGE, since="0000-00-00") is None
    assert repository.packages.package_source_id(_PACKAGE) == "12345"
    assert repository.packages.package_source_id(abandoned) == ""
