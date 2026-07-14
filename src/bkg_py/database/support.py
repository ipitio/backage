"""Shared configuration and staged-value validation for SQLite operations."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast


class DatabaseError(RuntimeError):
    """A database operation or staged record is invalid."""


def positive_env_int(name: str, default: int) -> int:
    """Return a positive environment integer or its default."""

    try:
        value = int(os.environ.get(name, ""))
    except ValueError:
        return default
    return value if value > 0 else default


def nonnegative_env_float(name: str, default: float) -> float:
    """Return a nonnegative environment float or its default."""

    try:
        value = float(os.environ.get(name, ""))
    except ValueError:
        return default
    return value if value >= 0 else default


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
