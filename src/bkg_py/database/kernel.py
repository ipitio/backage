"""Shared SQLite connection, schema, retry, and write-accounting kernel."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from threading import Lock
from typing import TypeVar

from .maintenance.metrics import DatabaseWriteTracker
from .schema import lifecycle as schema
from .settings import DatabaseSettings
from .support import DatabaseError, file_identity, transaction

_RETRYABLE_MESSAGES = (
    "database is locked",
    "database is busy",
    "database schema is locked",
    "locking protocol",
    "cannot commit transaction",
    "disk i/o error",
)
_Result = TypeVar("_Result")


class DatabaseKernel:
    """Own one database's connection policy and process-local state."""

    def __init__(
        self,
        settings: DatabaseSettings,
        *,
        check_stop: Callable[[], None] = lambda: None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.settings = settings
        self.write_tracker = DatabaseWriteTracker()
        self._check_stop = check_stop
        self._sleep = sleep
        self._schema_lock = Lock()
        self._schema_identity: tuple[int, int] | None = None

    def ensure_schema(self) -> None:
        """Lazily create normalized tables and their query indexes."""

        identity = file_identity(self.settings.path)
        if identity is not None and identity == self._schema_identity:
            return

        with self._schema_lock:
            identity = file_identity(self.settings.path)
            if identity is not None and identity == self._schema_identity:
                return

            def create(connection: sqlite3.Connection) -> None:
                with transaction(connection):
                    schema.ensure(
                        connection,
                        self.settings.owners_table,
                        self.settings.packages_table,
                        self.settings.versions_table,
                    )

            self.write(create)
            self._schema_identity = file_identity(self.settings.path)

    def read(self, operation: Callable[[sqlite3.Connection], _Result]) -> _Result:
        """Run one stop-aware read without retrying SQLite failures."""

        return self._run(operation, retry=False)

    def write(self, operation: Callable[[sqlite3.Connection], _Result]) -> _Result:
        """Run one stop-aware write with bounded retryable-error handling."""

        return self._run(operation, retry=True)

    def final_write(
        self,
        operation: Callable[[sqlite3.Connection], _Result],
    ) -> _Result:
        """Run a non-retrying write after finalization has deferred a stop."""

        return self._run(operation, retry=False, observe_stop=False)

    def check_stop(self) -> None:
        """Raise when the owning application has requested a cooperative stop."""

        self._check_stop()

    def _run(
        self,
        operation: Callable[[sqlite3.Connection], _Result],
        *,
        retry: bool,
        observe_stop: bool = True,
    ) -> _Result:
        attempt = 1
        while True:
            if observe_stop:
                self._check_stop()
            try:
                with self._connection() as connection:
                    return operation(connection)
            except sqlite3.Error as error:
                if (
                    not retry
                    or not _is_retryable(error)
                    or attempt >= self.settings.max_attempts
                ):
                    raise DatabaseError(str(error)) from error
                if observe_stop:
                    self._check_stop()
                self._sleep(self.settings.retry_delay_seconds)
                attempt += 1

    @contextmanager
    def _connection(self) -> Generator[sqlite3.Connection]:
        self.settings.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            self.settings.path,
            timeout=self.settings.busy_timeout_ms / 1000,
            isolation_level=None,
        )
        try:
            connection.execute(f"pragma busy_timeout = {self.settings.busy_timeout_ms}")
            connection.execute("pragma synchronous = normal")
            connection.execute("pragma foreign_keys = on")
            connection.execute("pragma journal_mode = wal")
            connection.execute("pragma locking_mode = normal")
            connection.execute("pragma temp_store = memory")
            connection.execute("pragma wal_autocheckpoint = 1000")
            connection.execute("pragma cache_size = -500000")
            yield connection
        finally:
            connection.close()


class DatabaseComponent:
    """Share one kernel with a focused repository implementation."""

    def __init__(self, kernel: DatabaseKernel) -> None:
        self.kernel = kernel

    @property
    def settings(self) -> DatabaseSettings:
        """Return immutable settings shared by the database composition."""

        return self.kernel.settings

    def ensure_schema(self) -> None:
        """Create or migrate the lazy normalized schema."""

        self.kernel.ensure_schema()

    def _run_read(
        self,
        operation: Callable[[sqlite3.Connection], _Result],
    ) -> _Result:
        return self.kernel.read(operation)

    def _run_write(
        self,
        operation: Callable[[sqlite3.Connection], _Result],
    ) -> _Result:
        return self.kernel.write(operation)

    def _run_final_write(
        self,
        operation: Callable[[sqlite3.Connection], _Result],
    ) -> _Result:
        return self.kernel.final_write(operation)


def _is_retryable(error: sqlite3.Error) -> bool:
    message = str(error).lower()
    return any(fragment in message for fragment in _RETRYABLE_MESSAGES)
