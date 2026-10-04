"""Read previous endpoints with bounded memory and disposable package lookups."""

import json
import sqlite3
import tempfile
from collections.abc import Callable, Generator, Iterator
from contextlib import closing, contextmanager
from importlib import import_module
from pathlib import Path
from typing import BinaryIO, Protocol, cast
from xml.etree.ElementTree import Element, ParseError

from .preservation import (
    needs_metric_fallback,
    package_identity,
    preserve_metrics,
    preserve_package,
)
from .values import JsonValue, PublicationError

type _JsonEvent = tuple[str, str, object]
_XML_PACKAGE_DEPTH = 2


class _JsonParser(Protocol):
    """Streaming JSON parser operations used by the baseline reader."""

    JSONError: type[Exception]

    def parse(self, source: BinaryIO, *, use_float: bool) -> Iterator[_JsonEvent]:
        """Read structural events without loading a complete aggregate."""

        raise NotImplementedError

    def items(
        self, events: Iterator[_JsonEvent], prefix: str, *, use_float: bool
    ) -> Iterator[object]:
        """Build one selected JSON item from the event stream."""

        raise NotImplementedError


class _XmlParser(Protocol):  # pylint: disable=too-few-public-methods
    """Safe XML parser operation used by the baseline reader."""

    def iterparse(
        self,
        source: BinaryIO,
        events: tuple[str, str],
        *,
        forbid_dtd: bool,
        forbid_entities: bool,
        forbid_external: bool,
    ) -> Iterator[tuple[str, Element]]:
        """Read elements while rejecting document types and entities."""

        raise NotImplementedError


# These libraries expose untyped APIs; keep their typed boundary local.
_JSON = cast(_JsonParser, import_module("ijson"))
_XML = cast(_XmlParser, import_module("defusedxml.ElementTree"))


def _json_events(
    source: BinaryIO, check_stop: Callable[[], None]
) -> Iterator[_JsonEvent]:
    for index, event in enumerate(_JSON.parse(source, use_float=True)):
        if index % 1024 == 0:
            check_stop()
        yield event


def _json_records(
    source: BinaryIO, check_stop: Callable[[], None]
) -> Iterator[JsonValue]:
    events = _json_events(source, check_stop)
    first = next(events, None)
    if first is None:
        raise PublicationError("empty previous JSON endpoint")

    def restored() -> Iterator[_JsonEvent]:
        yield first
        yield from events

    prefix = "item" if first[1] == "start_array" else ""
    for value in _JSON.items(restored(), prefix, use_float=True):
        yield cast(JsonValue, value)


def _xml_number(text: str, *, allow_float: bool) -> JsonValue:
    try:
        return int(text)
    except ValueError:
        if not allow_float:
            return text
    try:
        return float(text)
    except ValueError:
        return text


def _xml_value(element: Element) -> JsonValue:
    if len(element):
        value: dict[str, JsonValue] = {}
        for child in element:
            item = _xml_value(child)
            if child.tag in ("version", "tags"):
                items = value.setdefault(child.tag, [])
                if isinstance(items, list):
                    items.append(item)
            else:
                value[child.tag] = item
        return value
    text = element.text or ""
    if element.tag.startswith("raw_") or element.tag in ("id", "owner_id"):
        return _xml_number(text, allow_float=element.tag.startswith("raw_"))
    if element.tag in ("latest", "newest", "stale"):
        return text == "true"
    if element.tag in ("date", "observed_on") and text == "null":
        return None
    return text


def _xml_records(
    source: BinaryIO, check_stop: Callable[[], None]
) -> Iterator[JsonValue]:
    root: Element | None = None
    depth = 0
    aggregate = False
    events = _XML.iterparse(
        source,
        ("start", "end"),
        forbid_dtd=True,
        forbid_entities=True,
        forbid_external=True,
    )
    for index, (event, element) in enumerate(events):
        if index % 1024 == 0:
            check_stop()
        if event == "start":
            depth += 1
            if root is None:
                root = element
            continue
        if depth == _XML_PACKAGE_DEPTH and element.tag == "package" and len(element):
            aggregate = True
            yield _xml_value(element)
            if root is not None:
                root.remove(element)
        depth -= 1
    if root is None or root.tag != "xml":
        raise PublicationError("previous XML endpoint has no xml root")
    if not aggregate:
        yield _xml_value(root)


class PublicationBaseline:
    """Store one prior package at a time, rather than a second aggregate tree."""

    def __init__(
        self, connection: sqlite3.Connection, check_stop: Callable[[], None]
    ) -> None:
        self.connection = connection
        self.check_stop = check_stop
        self.root: dict[str, JsonValue] | None = None
        self.xml_fallback: Path | None = None

    def load(
        self, path: Path, check_stop: Callable[[], None], *, merge: bool = False
    ) -> None:
        """Validate the complete baseline before any endpoint can be replaced."""

        try:
            with path.open("rb") as source:
                records = (
                    _xml_records(source, check_stop)
                    if path.name.endswith(".xml")
                    else _json_records(source, check_stop)
                )
                for value in records:
                    check_stop()
                    self._store(value, merge=merge)
        except (
            ValueError,
            ParseError,
            _JSON.JSONError,
            sqlite3.IntegrityError,
        ) as error:
            raise PublicationError(
                f"cannot read publication baseline {path}: {error}"
            ) from error

    def _store(self, value: JsonValue, *, merge: bool) -> None:
        identity = package_identity(value)
        if identity is not None:
            if merge:
                self.connection.execute(
                    "INSERT INTO xml_seen (identity) VALUES (?)", (identity,)
                )
                previous = self._previous(identity)
                if previous is not None and isinstance(value, dict):
                    value = preserve_package(previous, value)
                    self.connection.execute(
                        "DELETE FROM baseline WHERE identity = ?", (identity,)
                    )
            self.connection.execute(
                "INSERT INTO baseline (identity, value) VALUES (?, ?)",
                (identity, json.dumps(value, ensure_ascii=False, allow_nan=False)),
            )
        elif isinstance(value, dict):
            self.root = (
                preserve_metrics(self.root, value)
                if merge and self.root is not None
                else value
            )

    def _previous(self, identity: str) -> dict[str, JsonValue] | None:
        row = self.connection.execute(
            "SELECT value FROM baseline WHERE identity = ?", (identity,)
        ).fetchone()
        previous: JsonValue = json.loads(row[0]) if row is not None else None
        return previous if isinstance(previous, dict) else None

    def preserve(self, value: JsonValue) -> JsonValue:
        """Preserve matching metrics without resurrecting retired packages."""

        self.check_stop()
        if isinstance(value, list):
            return [self.preserve(item) for item in value]
        if not isinstance(value, dict):
            if self.connection.execute("SELECT 1 FROM baseline LIMIT 1").fetchone():
                raise PublicationError("refusing to replace a package with a scalar")
            return value
        result = self._preserve_record(value)
        if self.xml_fallback is not None and needs_metric_fallback(result):
            self.load(self.xml_fallback, self.check_stop, merge=True)
            self.xml_fallback = None
            result = self._preserve_record(result)
        if (
            package_identity(result) is not None
            and self.root is not None
            and preserve_metrics(result, self.root) != result
        ):
            raise PublicationError(
                "cannot preserve metrics from a previous endpoint "
                "with incomplete package identity"
            )
        return result

    def _preserve_record(self, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        identity = package_identity(value)
        if identity is not None:
            previous = self._previous(identity)
            return (
                preserve_package(value, previous)
                if isinstance(previous, dict)
                else value
            )
        if self.connection.execute("SELECT 1 FROM baseline LIMIT 1").fetchone():
            raise PublicationError(
                "refusing to replace a package with an incomplete identity"
            )
        return preserve_metrics(value, self.root) if self.root is not None else value


@contextmanager
def publication_baseline(
    destination: Path,
    check_stop: Callable[[], None],
    *,
    prefer_xml: bool = False,
) -> Generator[PublicationBaseline]:
    """Remove the temporary lookup on success, failure, and graceful stop."""

    xml_path = destination.with_suffix(".xml")
    if destination.name == ".json":
        xml_path = destination.with_name(".xml")
    path = xml_path if prefer_xml and xml_path.is_file() else destination
    if not path.is_file():
        path = xml_path
    with (
        tempfile.TemporaryDirectory(prefix="bkg-publication-") as directory,
        closing(sqlite3.connect(Path(directory) / "baseline.db")) as connection,
    ):
        connection.execute(
            "CREATE TABLE baseline (identity TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.execute("CREATE TABLE xml_seen (identity TEXT PRIMARY KEY)")
        baseline = PublicationBaseline(connection, check_stop)
        if path.is_file():
            check_stop()
            baseline.load(path, check_stop)
        if path != xml_path and xml_path.is_file():
            baseline.xml_fallback = xml_path
        yield baseline
