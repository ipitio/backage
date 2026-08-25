"""Repository methods for the lazy rotation-independent package catalog."""

from __future__ import annotations

from collections.abc import Iterable

from ..history import package_history
from ..kernel import DatabaseComponent
from ..models import PackageCatalogPath, PackageCatalogStatus
from . import packages as catalog


class PackageCatalogRepository(DatabaseComponent):
    """Provide catalog initialization and status operations."""

    def package_catalog_status(self) -> PackageCatalogStatus | None:
        """Return committed catalog state, or None before initialization."""

        self.ensure_schema()
        return self._run_read(catalog.status)

    def initialize_package_catalog(
        self,
        paths: Iterable[PackageCatalogPath],
        source_revision: str,
        initialized_at: str,
    ) -> PackageCatalogStatus:
        """Atomically seed the catalog from tracked generated package paths."""

        self.ensure_schema()
        return self._run_write(
            lambda connection: catalog.initialize(
                connection,
                package_history.PACKAGE_HISTORY_VIEW,
                paths,
                source_revision,
                initialized_at,
            )
        )
