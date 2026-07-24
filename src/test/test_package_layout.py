"""Tests for the public domain package APIs."""

from bkg_py.database import DatabaseRepository, DatabaseSettings, PackageRef
from bkg_py.owners import OwnerBatchRequest, OwnerPageAdmissionResult
from bkg_py.owners import batch as owners_batch
from bkg_py.owners import pages as owners_pages


def test_database_package_exposes_repository_and_shared_values() -> None:
    """The database package is the cross-domain SQLite API."""

    assert DatabaseRepository.__module__ == "bkg_py.database.repository"
    assert DatabaseSettings.__module__ == "bkg_py.database.settings"
    assert PackageRef.__module__ == "bkg_py.database.models"


def test_owner_package_exposes_outer_run_operations() -> None:
    """The owners package provides the outer application-facing API."""

    assert OwnerBatchRequest is owners_batch.OwnerBatchRequest
    assert OwnerPageAdmissionResult is owners_pages.OwnerPageAdmissionResult
