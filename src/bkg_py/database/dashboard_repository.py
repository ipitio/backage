"""Repository boundary for bounded dashboard projections."""

from __future__ import annotations

import sqlite3
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

from . import dashboard
from .dashboard import DashboardProjection


class DashboardRepositoryMixin(ABC):  # pylint: disable=too-few-public-methods
    """Add current dashboard projection reads to the database repository."""

    @abstractmethod
    def ensure_schema(self) -> None:
        """Create or migrate the lazy normalized schema."""

        raise NotImplementedError

    @abstractmethod
    def _run_read(self, operation: Callable[[sqlite3.Connection], Any]) -> Any:
        raise NotImplementedError

    def dashboard_projection(self, today: str) -> DashboardProjection:
        """Return one bounded projection from the current catalog snapshot."""

        self.ensure_schema()
        return self._run_read(lambda connection: dashboard.project(connection, today))
