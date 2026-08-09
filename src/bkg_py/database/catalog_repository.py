"""Repository methods for the lazy rotation-independent package catalog."""

from __future__ import annotations

import sqlite3
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable
from typing import Any

from . import catalog, package_history
from .models import PackageCatalogPath, PackageCatalogStatus
from .settings import DatabaseSettings


class PackageCatalogRepositoryMixin(ABC):
    """Add catalog initialization and status operations to the repository."""

    settings: DatabaseSettings

    @abstractmethod
    def ensure_schema(self) -> None:
        """Create or migrate the lazy normalized schema."""

        raise NotImplementedError

    @abstractmethod
    def _run_read(self, operation: Callable[[sqlite3.Connection], Any]) -> Any:
        raise NotImplementedError

    @abstractmethod
    def _run_write(self, operation: Callable[[sqlite3.Connection], Any]) -> Any:
        raise NotImplementedError

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
