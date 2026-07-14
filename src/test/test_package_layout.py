"""Tests for domain package APIs and transitional import paths."""

from bkg_py import (
    database_batch_progress,
    database_models,
    database_owner_identities,
    database_owner_plans,
    database_owner_repository,
    database_owner_scans,
    database_package_plans,
    database_packages,
    database_schema,
    database_settings,
    database_support,
    database_values,
    database_version_stages,
    owner_batch,
    owner_commands,
    owner_lifecycle,
    owner_operations,
    owner_package_updates,
    owner_pages,
    owner_publication,
    owner_queue,
    owner_queue_operations,
    owner_scan_pages,
    owner_updates,
    render_sql,
    schema_sql,
)
from bkg_py.database import (
    DatabaseRepository,
    DatabaseSettings,
    PackageRef,
    batch_progress,
    models,
    owner_identities,
    owner_plans,
    owner_repository,
    owner_scans,
    package_plans,
    packages,
    schema,
    support,
    values,
    version_stages,
)
from bkg_py.database import (
    render_sql as database_render_sql,
)
from bkg_py.database import (
    schema_sql as database_schema_sql,
)
from bkg_py.owners import OwnerBatchRequest, OwnerPageAdmissionResult
from bkg_py.owners import batch as owners_batch
from bkg_py.owners import commands as owners_commands
from bkg_py.owners import lifecycle as owners_lifecycle
from bkg_py.owners import operations as owners_operations
from bkg_py.owners import package_updates as owners_package_updates
from bkg_py.owners import pages as owners_pages
from bkg_py.owners import publication as owners_publication
from bkg_py.owners import queue as owners_queue
from bkg_py.owners import queue_operations as owners_queue_operations
from bkg_py.owners import scan_pages as owners_scan_pages
from bkg_py.owners import updates as owners_updates


def test_database_package_exposes_repository_and_shared_values() -> None:
    """The database package is the cross-domain SQLite API."""

    assert DatabaseRepository.__module__ == "bkg_py.database.repository"
    assert DatabaseSettings.__module__ == "bkg_py.database.settings"
    assert PackageRef.__module__ == "bkg_py.database.models"


def test_database_compatibility_modules_reexport_domain_implementations() -> None:
    """Previous database imports remain usable during the migration."""

    assert database_models.PackageRef is PackageRef
    assert database_settings.DatabaseSettings is DatabaseSettings
    assert database_support.DatabaseError is support.DatabaseError
    assert database_values.package_values is values.package_values
    assert database_batch_progress.bootstrap is batch_progress.bootstrap
    assert (
        database_owner_identities.OwnerIdentityRepositoryMixin
        is owner_identities.OwnerIdentityRepositoryMixin
    )
    assert database_owner_plans.owner_refresh_plan is owner_plans.owner_refresh_plan
    assert (
        database_owner_repository.OwnerScanRepositoryMixin
        is owner_repository.OwnerScanRepositoryMixin
    )
    assert database_owner_scans.complete is owner_scans.complete
    assert database_package_plans.load is package_plans.load
    assert database_packages.write is packages.write
    assert database_schema.ensure is schema.ensure
    assert database_version_stages.flush is version_stages.flush
    assert render_sql.PACKAGE_SNAPSHOT_SQL is database_render_sql.PACKAGE_SNAPSHOT_SQL
    assert schema_sql.SCHEMA_SQL is database_schema_sql.SCHEMA_SQL
    assert database_models.OwnerScanResult is models.OwnerScanResult


def test_owner_package_exposes_outer_run_operations() -> None:
    """The owners package provides the outer application-facing API."""

    assert OwnerBatchRequest is owners_batch.OwnerBatchRequest
    assert OwnerPageAdmissionResult is owners_pages.OwnerPageAdmissionResult


def test_owner_compatibility_modules_reexport_domain_implementations() -> None:
    """Previous owner imports remain usable during the migration."""

    assert owner_batch.OwnerBatchService is owners_batch.OwnerBatchService
    assert owner_commands.run_owner is owners_commands.run_owner
    assert (
        owner_lifecycle.OwnerLifecycleService is owners_lifecycle.OwnerLifecycleService
    )
    assert (
        owner_operations.OwnerUpdateOperation is owners_operations.OwnerUpdateOperation
    )
    assert (
        owner_package_updates.OwnerPackageRefreshService
        is owners_package_updates.OwnerPackageRefreshService
    )
    assert owner_pages.admit_owner_page is owners_pages.admit_owner_page
    assert (
        owner_publication.OwnerPublicationService
        is owners_publication.OwnerPublicationService
    )
    assert owner_queue.OwnerQueueSelector is owners_queue.OwnerQueueSelector
    assert (
        owner_queue_operations.OwnerQueuePreparationService
        is owners_queue_operations.OwnerQueuePreparationService
    )
    assert (
        owner_scan_pages.OwnerScanPageService is owners_scan_pages.OwnerScanPageService
    )
    assert owner_updates.OwnerScanService is owners_updates.OwnerScanService
