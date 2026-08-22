"""Shared configuration and staged-value validation for SQLite operations."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast


class DatabaseError(RuntimeError):
    """A database operation or staged record is invalid."""


class SqlFragment(str):
    """A statement fragment constructed by trusted database code."""


class SqlIdentifier(SqlFragment):
    """A SQLite identifier quoted before statement construction."""

    def __new__(cls, value: str) -> SqlIdentifier:
        if "\x00" in value:
            raise DatabaseError("SQLite identifiers cannot contain NUL")
        return str.__new__(cls, f'"{value.replace(chr(34), chr(34) * 2)}"')


def sql(statement: str, /, **fragments: SqlFragment) -> str:
    """Substitute only trusted fragments into a SQL statement."""

    return statement.format_map(fragments)


def required_text(value: Mapping[str, Any], key: str) -> str:
    """Return a required nonempty text field."""

    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise DatabaseError(f"stage field {key!r} must be nonempty text")
    return item


def required_string(value: Mapping[str, Any], key: str) -> str:
    """Return a required string field that may be empty."""

    item = value.get(key)
    if not isinstance(item, str):
        raise DatabaseError(f"stage field {key!r} must be text")
    return item


def optional_text(value: Mapping[str, Any], key: str) -> str:
    """Return an optional text field with an empty default."""

    item = value.get(key, "")
    if not isinstance(item, str):
        raise DatabaseError(f"stage field {key!r} must be text")
    return item


def required_int(value: Mapping[str, Any], key: str) -> int:
    """Return a required integer field, excluding booleans."""

    item = value.get(key)
    if isinstance(item, bool) or not isinstance(item, int):
        raise DatabaseError(f"stage field {key!r} must be an integer")
    return item


def load_object(path: Path) -> dict[str, Any]:
    """Load a JSON object from one staged metadata file."""

    try:
        value: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise DatabaseError(f"invalid stage file {path}: {error}") from error
    if not isinstance(value, dict):
        raise DatabaseError(f"stage file {path} must contain an object")
    mapping = cast(dict[object, object], value)
    if not all(isinstance(key, str) for key in mapping):
        raise DatabaseError(f"stage file {path} must contain an object")
    return cast(dict[str, Any], mapping)


def file_identity(path: Path) -> tuple[int, int] | None:
    """Return the device and inode for an existing database path."""

    try:
        status = path.stat()
    except FileNotFoundError:
        return None
    return status.st_dev, status.st_ino


def table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    """Return whether one main-schema table exists."""

    return (
        connection.execute(
            "select 1 from sqlite_master where type = 'table' and name = ? limit 1",
            (table_name,),
        ).fetchone()
        is not None
    )


@contextmanager
def transaction(connection: sqlite3.Connection) -> Generator[None]:
    """Commit one immediate SQLite transaction or roll it back on failure."""

    connection.execute("begin immediate")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    connection.commit()
