"""Composition of focused repositories sharing one SQLite kernel."""

import time
from collections.abc import Callable

from .catalog.dashboard_repository import DashboardRepository
from .catalog.package_repository import PackageCatalogRepository
from .history.repository import HistoryRepository
from .kernel import DatabaseKernel
from .maintenance.metrics_repository import DatabaseMetricsRepository
from .maintenance.rotation_repository import DatabaseRotationRepository
from .owner.identities import OwnerIdentityRepository
from .owner.queue_repository import OwnerQueueRepository
from .owner.scan_repository import OwnerScanRepository
from .package.repository import PackageRepository
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
