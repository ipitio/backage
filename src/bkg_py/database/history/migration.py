"""Retryable transaction adapter for bounded history migrations."""

import sqlite3
from collections.abc import Callable
from typing import Any

from ..support import transaction

WriteOperation = Callable[[Callable[[sqlite3.Connection], Any]], Any]


def migrate[MigrationProgress](
    run_write: WriteOperation,
    legacy_table: str,
    row_limit: int,
    migrate_batch: Callable[
        [sqlite3.Connection, str, int],
        MigrationProgress,
    ],
) -> MigrationProgress:
    """Commit one migration batch through the repository writer."""

    def operation(connection: sqlite3.Connection) -> MigrationProgress:
        with transaction(connection):
            return migrate_batch(connection, legacy_table, row_limit)

    return run_write(operation)
