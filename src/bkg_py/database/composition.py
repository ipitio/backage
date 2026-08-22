"""Composition of focused repositories sharing one SQLite kernel."""

from __future__ import annotations

import time
from collections.abc import Callable

from .catalog_repository import PackageCatalogRepository
from .dashboard_repository import DashboardRepository
from .history_repository import HistoryRepository
from .kernel import DatabaseKernel
from .metrics_repository import DatabaseMetricsRepository
from .owner_identities import OwnerIdentityRepository
from .owner_queue_repository import OwnerQueueRepository
from .owner_repository import OwnerScanRepository
from .package_repository import PackageRepository
from .rotation_repository import DatabaseRotationRepository
from .settings import DatabaseSettings


# pylint: disable-next=too-many-instance-attributes,too-few-public-methods
class DatabaseRepositories:
    """Expose focused repositories backed by one connection policy and schema."""

    def __init__(
        self,
        settings: DatabaseSettings,
        *,
        check_stop: Callable[[], None] = lambda: None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        kernel = DatabaseKernel(settings, check_stop=check_stop, sleep=sleep)
        self.kernel = kernel
        self.packages = PackageRepository(kernel)
        self.owners = OwnerScanRepository(kernel)
        self.owner_identities = OwnerIdentityRepository(kernel)
        self.owner_queue = OwnerQueueRepository(kernel)
        self.catalog = PackageCatalogRepository(kernel)
        self.dashboard = DashboardRepository(kernel)
        self.history = HistoryRepository(kernel)
        self.metrics = DatabaseMetricsRepository(kernel)
        self.rotations = DatabaseRotationRepository(kernel)
