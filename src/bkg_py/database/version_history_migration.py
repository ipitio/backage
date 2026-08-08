"""Transactional adapter for bounded version-history migration."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Generator
from contextlib import contextmanager
from typing import Any

from . import version_history

WriteOperation = Callable[[Callable[[sqlite3.Connection], Any]], Any]


def migrate(
    run_write: WriteOperation,
    legacy_table: str,
    row_limit: int,
) -> version_history.VersionHistoryMigration:
    """Move one batch through the repository's retryable writer."""

    def operation(
        connection: sqlite3.Connection,
    ) -> version_history.VersionHistoryMigration:
        with _transaction(connection):
            return version_history.migrate_batch(
                connection,
                legacy_table,
                row_limit,
            )

    return run_write(operation)


@contextmanager
def _transaction(connection: sqlite3.Connection) -> Generator[None]:
    connection.execute("begin immediate")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    connection.commit()
