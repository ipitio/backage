"""Repository methods for bounded database finalization measurements."""

from __future__ import annotations

from . import metrics, package_history
from .kernel import DatabaseComponent
from .metrics import (
    DatabaseMetricSample,
    DatabaseStorageMetrics,
    DatabaseWriteCounts,
)


class DatabaseMetricsRepository(DatabaseComponent):
    """Provide bounded database finalization measurements."""

    def database_storage_metrics(self) -> DatabaseStorageMetrics:
        """Capture bounded storage measurements for the checkpointed database."""

        self.ensure_schema()
        return self._run_read(
            lambda connection: metrics.capture(
                connection,
                self.settings.path,
                package_history.PACKAGE_HISTORY_VIEW,
                self.settings.versions_table,
            )
        )

    def database_write_counts(self) -> DatabaseWriteCounts:
        """Return normalized package and version writes by this process."""

        return self.kernel.write_tracker.counts()

    def record_database_metric_sample(self, sample: DatabaseMetricSample) -> None:
        """Persist one bounded daily finalization measurement."""

        self.ensure_schema()
        self._run_write(lambda connection: metrics.record(connection, sample))

    def database_metric_samples(self) -> tuple[DatabaseMetricSample, ...]:
        """Return persisted daily finalization measurements."""

        self.ensure_schema()
        return self._run_read(metrics.load_samples)
