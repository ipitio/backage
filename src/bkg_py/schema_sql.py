"""Compatibility exports for normalized database schema SQL."""

from .database.schema_sql import (
    OWNER_SCAN_SCHEMA_MIGRATIONS,
    PACKAGE_PRIMARY_KEY,
    PACKAGES_TABLE_SQL,
    SCHEMA_SQL,
)

__all__ = [
    "OWNER_SCAN_SCHEMA_MIGRATIONS",
    "PACKAGES_TABLE_SQL",
    "PACKAGE_PRIMARY_KEY",
    "SCHEMA_SQL",
]
