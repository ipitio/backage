"""Validate generated index files with the same results as the shell validator."""

from __future__ import annotations

import json
import xml.etree.ElementTree as element_tree
from pathlib import Path

from .result import ExitStatus

_MISSING = object()


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _last_json_value(text: str) -> object:
    decoder = json.JSONDecoder(parse_constant=_reject_json_constant)
    position = 0
    last_value: object = _MISSING

    while True:
        while position < len(text) and text[position].isspace():
            position += 1
        if position >= len(text):
            break
        last_value, position = decoder.raw_decode(text, position)

    if last_value is _MISSING:
        raise ValueError("empty JSON input")
    return last_value


def _is_valid_json(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8")
        last_value = _last_json_value(text)
    except OSError, UnicodeError, ValueError:
        return False
    return last_value is not None and last_value is not False


def _is_valid_xml(path: Path) -> bool:
    try:
        element_tree.parse(path)
    except OSError, element_tree.ParseError:
        return False
    return True


def validate_generated_file(filename: str) -> ExitStatus:
    """Report invalid generated JSON or XML and remove empty output files."""

    path = Path(filename) if filename else None
    if path is None:
        print(f"Empty file: {filename}")
        return ExitStatus.SUCCESS

    try:
        has_content = path.stat().st_size > 0
    except OSError:
        has_content = False

    if not has_content:
        print(f"Empty file: {filename}")
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass
        return ExitStatus.SUCCESS

    if filename.endswith(".json"):
        if not _is_valid_json(path):
            print(f"Invalid json: {filename}")
        return ExitStatus.SUCCESS

    if not _is_valid_xml(path):
        print(f"Invalid xml: {filename}")
    return ExitStatus.SUCCESS
