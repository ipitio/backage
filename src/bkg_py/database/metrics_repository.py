"""Repository methods for bounded database finalization measurements."""

from __future__ import annotations

import sqlite3
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

from . import metrics
from .metrics import (
    DatabaseMetricSample,
    DatabaseStorageMetrics,
    DatabaseWriteCounts,
    DatabaseWriteTracker,
)
from .settings import DatabaseSettings


class DatabaseMetricsRepositoryMixin(ABC):
    """Add storage measurement operations to the shared repository."""

    settings: DatabaseSettings
    _write_tracker: DatabaseWriteTracker

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

    def database_storage_metrics(self) -> DatabaseStorageMetrics:
        """Capture bounded storage measurements for the checkpointed database."""

        self.ensure_schema()
        return self._run_read(
            lambda connection: metrics.capture(
                connection,
                self.settings.path,
                self.settings.packages_table,
                self.settings.versions_table,
            )
        )

    def database_write_counts(self) -> DatabaseWriteCounts:
        """Return normalized package and version writes by this process."""

        return self._write_tracker.counts()

    def record_database_metric_sample(self, sample: DatabaseMetricSample) -> None:
        """Persist one bounded daily finalization measurement."""

        self.ensure_schema()
        self._run_write(lambda connection: metrics.record(connection, sample))

    def database_metric_samples(self) -> tuple[DatabaseMetricSample, ...]:
        """Return persisted daily finalization measurements."""

        self.ensure_schema()
        return self._run_read(metrics.load_samples)
