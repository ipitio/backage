"""Generate bounded JSON and XML publication files."""

from __future__ import annotations

import json
import math
import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from .files import atomic_path

_XML_PREFIX = '<?xml version="1.0" encoding="UTF-8"?><xml>'
_XML_SUFFIX = "</xml>"
_EMPTY_XML = f"{_XML_PREFIX}{_XML_SUFFIX}\n".encode()
_WRITE_CHUNK_SIZE = 1024 * 1024
_MAX_TRIM_COUNT = 65536
_CONTROL_CHARACTER_LIMIT = 32

JsonValue = dict[str, "JsonValue"] | list["JsonValue"] | str | int | float | bool | None
StopCheck = Callable[[], None]


class PublicationError(ValueError):
    """A generated file cannot be parsed or serialized."""


@dataclass(frozen=True)
class PublicationLimits:
    """Soft trimming and hard publication byte limits."""

    maximum_bytes: int = 50_000_000
    hard_maximum_bytes: int = 100_000_000

    @classmethod
    def from_env(cls) -> PublicationLimits:
        """Read positive byte limits from the current environment."""

        return cls(
            maximum_bytes=_positive_env_int(
                "BKG_JSON_XML_MAX_BYTES",
                cls.maximum_bytes,
            ),
            hard_maximum_bytes=_positive_env_int(
                "BKG_JSON_XML_HARD_MAX_BYTES",
                cls.hard_maximum_bytes,
            ),
        )


@dataclass(frozen=True)
class PublicationResult:
    """Sizes and trimming state for one published JSON/XML pair."""

    json_size: int
    xml_size: int
    trimmed: bool


@dataclass
class _TrimState:
    value: JsonValue
    json_output: bytes
    target_json_size: int
    delete_count: int = 1
    last_xml_size: int = -1
    xml_size: int | None = None
    trimmed: bool = False


@dataclass(frozen=True)
class _PreparedPublication:
    json_output: bytes
    xml_value: JsonValue
    xml_is_empty: bool
    xml_size: int
    trimmed: bool


def _positive_env_int(name: str, default: int) -> int:
    value = os.environ.get(name, "")
    try:
        parsed = int(value)
    except ValueError:
        return default
    return parsed if parsed > 0 else default


def _reject_json_constant(value: str) -> None:
    raise PublicationError(f"invalid JSON constant: {value}")


def _load_json(source: bytes) -> JsonValue:
    try:
        return json.loads(source, parse_constant=_reject_json_constant)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PublicationError(f"invalid JSON: {error}") from error


def _compact_json(value: JsonValue) -> bytes:
    try:
        text = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as error:
        raise PublicationError(f"cannot serialize JSON: {error}") from error
    return f"{text}\n".encode()


def _xml_text(value: str) -> str:
    pieces: list[str] = []
    start = 0
    replacements = {
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&#34;",
        "'": "&#39;",
        "\t": "&#x9;",
        "\r": "&#xD;",
    }

    for index, character in enumerate(value):
        replacement = replacements.get(character)
        if (
            replacement is None
            and ord(character) < _CONTROL_CHARACTER_LIMIT
            and character != "\n"
        ):
            replacement = "\ufffd"
        if replacement is None:
            continue
        if start < index:
            pieces.append(value[start:index])
        pieces.append(replacement)
        start = index + 1

    if not pieces:
        return value
    pieces.append(value[start:])
    return "".join(pieces)


def _scalar_text(value: JsonValue) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return _xml_text(value)
    if isinstance(value, (int, float)):
        return str(value)
    raise PublicationError(f"unsupported XML scalar: {type(value).__name__}")


def _xml_elements(name: str, value: JsonValue) -> Iterator[str]:
    if isinstance(value, list):
        for item in value:
            yield from _xml_elements(name, item)
        return

    yield f"<{name}>"
    if isinstance(value, dict):
        for child_name, child_value in value.items():
            yield from _xml_elements(child_name, child_value)
    else:
        yield _scalar_text(value)
    yield f"</{name}>"


def xml_chunks(value: JsonValue) -> Iterator[str]:
    """Yield XML chunks matching the former yq-based endpoint shape."""

    yield _XML_PREFIX
    if isinstance(value, list):
        yield from _xml_elements("package", value)
    elif isinstance(value, dict):
        for name, child in value.items():
            yield from _xml_elements(name, child)
    else:
        yield _scalar_text(value)
    yield _XML_SUFFIX


def _xml_size(value: JsonValue, check_stop: StopCheck) -> int:
    size = 0
    for index, chunk in enumerate(xml_chunks(value)):
        if index % 1024 == 0:
            check_stop()
        size += len(chunk.encode())
    return size


def _numeric(value: JsonValue) -> int | float:
    if isinstance(value, bool) or value is None:
        return 0
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        try:
            parsed = float(value)
        except ValueError:
            return 0
        if not math.isfinite(parsed):
            return 0
        return int(parsed) if parsed.is_integer() else parsed
    return 0


def _identifier_key(value: JsonValue) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _version_list(value: JsonValue) -> list[JsonValue]:
    if not isinstance(value, dict):
        return []
    versions = value.get("version", [])
    return versions if isinstance(versions, list) else []


def _version_id(value: JsonValue) -> JsonValue:
    return value.get("id") if isinstance(value, dict) else None


def _is_protected_version(value: JsonValue) -> bool:
    return isinstance(value, dict) and (
        value.get("latest") is True or value.get("newest") is True
    )


def _trim_version_holder(value: JsonValue, count: int) -> JsonValue:
    if not isinstance(value, dict):
        return value
    versions = _version_list(value)
    if not versions:
        return value

    ordered = versions
    if any(
        _numeric(_version_id(versions[index - 1]))
        > _numeric(_version_id(versions[index]))
        for index in range(1, len(versions))
    ):
        ordered = sorted(versions, key=lambda item: _numeric(_version_id(item)))

    candidates = [item for item in ordered if _is_protected_version(item)] + ordered[
        count:
    ]
    unique: dict[str, JsonValue] = {}
    for item in sorted(candidates, key=lambda item: _identifier_key(_version_id(item))):
        unique.setdefault(_identifier_key(_version_id(item)), item)

    result = dict(value)
    result["version"] = sorted(
        unique.values(),
        key=lambda item: _numeric(_version_id(item)),
    )
    return result


def _holder_with_most_versions(values: list[JsonValue]) -> int | None:
    if not values:
        return None
    return max(
        range(len(values)),
        key=lambda index: (len(_version_list(values[index])), index),
    )


def _has_versions(value: JsonValue) -> bool:
    if isinstance(value, list):
        return any(_version_list(item) for item in value)
    if isinstance(value, dict):
        packages = value.get("package")
        if isinstance(packages, list):
            return any(_version_list(item) for item in packages)
        return bool(_version_list(value))
    return False


def _trim_largest_versions(value: JsonValue, count: int) -> JsonValue:
    if isinstance(value, list):
        index = _holder_with_most_versions(value)
        if index is None or not _version_list(value[index]):
            return value
        result = list(value)
        result[index] = _trim_version_holder(result[index], count)
        return result

    if isinstance(value, dict):
        packages = value.get("package")
        if isinstance(packages, list):
            index = _holder_with_most_versions(packages)
            if index is None or not _version_list(packages[index]):
                return value
            result = dict(value)
            package_result = list(packages)
            package_result[index] = _trim_version_holder(
                package_result[index],
                count,
            )
            result["package"] = package_result
            return result
        return _trim_version_holder(value, count)

    return value


def _download_date_key(value: JsonValue) -> tuple[int | float, str]:
    if not isinstance(value, dict):
        return 0, ""
    date = value.get("date", "")
    return _numeric(value.get("raw_downloads", 0)), (
        date if isinstance(date, str) else ""
    )


def _drop_one_from_list(values: list[JsonValue]) -> list[JsonValue]:
    if not values:
        return []
    index = min(range(len(values)), key=lambda item: _download_date_key(values[item]))
    return values[:index] + values[index + 1 :]


def _drop_one(value: JsonValue) -> JsonValue:
    if isinstance(value, list):
        return _drop_one_from_list(value)
    if isinstance(value, dict):
        packages = value.get("package")
        if isinstance(packages, list):
            result = dict(value)
            result["package"] = _drop_one_from_list(packages)
            return result
        if not value:
            return value
        key = min(value, key=lambda item: _download_date_key(value[item]))
        return {item: child for item, child in value.items() if item != key}
    return value


def _trim_once(value: JsonValue, count: int) -> JsonValue:
    return (
        _trim_largest_versions(value, count)
        if _has_versions(value)
        else _drop_one(value)
    )


def _write_bytes(path: Path, data: bytes, check_stop: StopCheck) -> None:
    with path.open("wb") as file:
        for start in range(0, len(data), _WRITE_CHUNK_SIZE):
            check_stop()
            file.write(data[start : start + _WRITE_CHUNK_SIZE])


def _write_xml(path: Path, value: JsonValue, check_stop: StopCheck) -> None:
    with path.open("wb") as file:
        for index, chunk in enumerate(xml_chunks(value)):
            if index % 1024 == 0:
                check_stop()
            file.write(chunk.encode())


def _xml_path(source: Path) -> Path:
    stem = source.name.rsplit(".", maxsplit=1)[0]
    return source.with_name(f"{stem}.xml")


def _publication_complete(
    state: _TrimState,
    limits: PublicationLimits,
    check_stop: StopCheck,
) -> bool:
    json_size = len(state.json_output)
    if json_size >= state.target_json_size:
        state.delete_count = min(state.delete_count * 2, _MAX_TRIM_COUNT)
        return False

    state.xml_size = _xml_size(state.value, check_stop)
    if state.xml_size < limits.maximum_bytes:
        return True
    if state.xml_size == state.last_xml_size and state.last_xml_size >= 0:
        return True

    state.last_xml_size = state.xml_size
    if json_size > 0 and state.xml_size > 0:
        next_target = limits.maximum_bytes * json_size // state.xml_size
        next_target = max(1, next_target * 95 // 100)
        state.target_json_size = min(state.target_json_size, next_target)
    state.delete_count = min(state.delete_count * 2, _MAX_TRIM_COUNT)
    return False


def _advance_trim(state: _TrimState) -> bool:
    json_size = len(state.json_output)
    candidate = _trim_once(state.value, state.delete_count)
    candidate_json = _compact_json(candidate)

    if len(candidate_json) >= json_size:
        if state.delete_count < _MAX_TRIM_COUNT:
            state.delete_count = min(state.delete_count * 2, _MAX_TRIM_COUNT)
            return True
        candidate = _drop_one(state.value)
        candidate_json = _compact_json(candidate)
        if len(candidate_json) >= json_size:
            return False

    state.value = candidate
    state.json_output = candidate_json
    state.xml_size = None
    state.trimmed = True
    return True


def _prepare_publication(
    original_json: bytes,
    limits: PublicationLimits,
    check_stop: StopCheck,
) -> _PreparedPublication:
    state = _TrimState(
        value=_load_json(original_json),
        json_output=original_json,
        target_json_size=limits.maximum_bytes,
    )

    while True:
        check_stop()
        if _publication_complete(state, limits, check_stop):
            break
        check_stop()
        if not _advance_trim(state):
            break

    if state.xml_size is None:
        state.xml_size = _xml_size(state.value, check_stop)

    json_output = state.json_output
    if len(json_output) >= limits.hard_maximum_bytes:
        json_output = b"{}\n"

    xml_is_empty = state.xml_size >= limits.hard_maximum_bytes
    xml_size = len(_EMPTY_XML) if xml_is_empty else state.xml_size
    return _PreparedPublication(
        json_output=json_output,
        xml_value=state.value,
        xml_is_empty=xml_is_empty,
        xml_size=xml_size,
        trimmed=state.trimmed,
    )


def write_xml_file(
    source: Path,
    check_stop: StopCheck,
    destination: Path | None = None,
) -> int:
    """Convert one JSON file into an atomically replaced XML endpoint."""

    if not source.is_file():
        raise PublicationError(f"missing JSON file: {source}")
    check_stop()
    value = _load_json(source.read_bytes())
    xml_path = destination or _xml_path(source)
    with atomic_path(xml_path) as temporary_path:
        _write_xml(temporary_path, value, check_stop)
        check_stop()
    return xml_path.stat().st_size


def publish_json_file(
    source: Path,
    check_stop: StopCheck,
    limits: PublicationLimits | None = None,
) -> PublicationResult:
    """Trim and atomically publish a JSON file with its XML representation."""

    if not source.is_file():
        raise PublicationError(f"missing JSON file: {source}")
    publication_limits = limits or PublicationLimits.from_env()
    prepared = _prepare_publication(
        source.read_bytes(),
        publication_limits,
        check_stop,
    )

    xml_path = _xml_path(source)
    check_stop()
    with (
        atomic_path(xml_path) as temporary_xml,
        atomic_path(source) as temporary_json,
    ):
        _write_bytes(temporary_json, prepared.json_output, check_stop)
        if prepared.xml_is_empty:
            _write_bytes(temporary_xml, _EMPTY_XML, check_stop)
        else:
            _write_xml(temporary_xml, prepared.xml_value, check_stop)
        check_stop()
    check_stop()

    return PublicationResult(
        json_size=len(prepared.json_output),
        xml_size=prepared.xml_size,
        trimmed=prepared.trimmed,
    )
