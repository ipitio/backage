"""Check generated JSON and XML without modifying the input files."""

import sys
from collections.abc import Iterator
from importlib import import_module
from pathlib import Path
from typing import BinaryIO, Protocol, cast
from xml.etree.ElementTree import Element, ParseError

from .result import ExitStatus


class _JsonParser(Protocol):  # pylint: disable=too-few-public-methods
    """JSON syntax events without constructing the complete document."""

    JSONError: type[Exception]

    def basic_parse(
        self, source: BinaryIO, *, multiple_values: bool
    ) -> Iterator[tuple[str, object]]:
        """Parse exactly one JSON document to the end of the input."""

        raise NotImplementedError


class _XmlParser(Protocol):  # pylint: disable=too-few-public-methods
    """Safe XML events used only while checking a file."""

    def iterparse(
        self,
        source: BinaryIO,
        events: tuple[str, str],
        *,
        forbid_dtd: bool,
        forbid_entities: bool,
        forbid_external: bool,
    ) -> Iterator[tuple[str, Element]]:
        """Read structural events without document types or entities."""

        raise NotImplementedError


def _is_valid_json(source: BinaryIO) -> bool:
    parser = cast(_JsonParser, import_module("ijson"))
    try:
        for _ in parser.basic_parse(source, multiple_values=False):
            pass
    except parser.JSONError, UnicodeError, ValueError:
        return False
    return True


def _is_valid_xml(source: BinaryIO) -> bool:
    parser = cast(_XmlParser, import_module("defusedxml.ElementTree"))
    parents: list[Element] = []
    try:
        for event, element in parser.iterparse(
            source,
            ("start", "end"),
            forbid_dtd=True,
            forbid_entities=True,
            forbid_external=True,
        ):
            if event == "start":
                parents.append(element)
            else:
                parents.pop()
                if parents:
                    parents[-1].remove(element)
                element.clear()
    except ParseError, UnicodeError, ValueError, LookupError:
        return False
    return True


def validate_generated_file(filename: str) -> ExitStatus:
    """Return failure for unreadable, empty, malformed, or unsafe input."""

    if not filename:
        print(f"Empty file: {filename}", file=sys.stderr)
        return ExitStatus.NON_FATAL

    path = Path(filename)
    file_type = "json" if path.suffix.casefold() == ".json" else "xml"
    try:
        with path.open("rb") as source:
            if not source.read(1):
                print(f"Empty file: {filename}", file=sys.stderr)
                return ExitStatus.NON_FATAL
            source.seek(0)
            valid = (
                _is_valid_json(source) if file_type == "json" else _is_valid_xml(source)
            )
    except OSError as error:
        print(f"Cannot read file: {filename}: {error}", file=sys.stderr)
        return ExitStatus.NON_FATAL
    if not valid:
        print(f"Invalid {file_type}: {filename}", file=sys.stderr)
        return ExitStatus.NON_FATAL
    return ExitStatus.SUCCESS
