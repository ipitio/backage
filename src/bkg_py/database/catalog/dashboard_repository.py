"""Repository boundary for bounded dashboard projections."""

from __future__ import annotations

from ..kernel import DatabaseComponent
from . import dashboard
from .dashboard import DashboardProjection


class DashboardRepository(DatabaseComponent):  # pylint: disable=too-few-public-methods
    """Provide current dashboard projection reads."""

    def dashboard_projection(self, today: str) -> DashboardProjection:
        """Return one bounded projection from the current catalog snapshot."""

        self.ensure_schema()
        return self._run_read(lambda connection: dashboard.project(connection, today))
