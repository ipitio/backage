"""Version-page parsing and container manifest helpers."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import cast

_DOWNLOAD_LABELS = {
    "total": "Total downloads",
    "month": "Last 30 days",
    "week": "Last week",
    "day": "Today",
}
_METRIC_SUFFIXES = "KMBTPEZY"
_METRIC_PATTERN = re.compile(r"^([0-9]+(?:\.[0-9]+)?)([A-Za-z]?)$")
_CODE_PATTERN = re.compile(r"<code.*?>", re.DOTALL)


@dataclass(frozen=True)
class DownloadMetrics:
    """Download counters extracted from one version page."""

    total: int
    month: int
    week: int
    day: int


@dataclass(frozen=True)
class ManifestSizeResult:
    """Container manifest size calculation outcome."""

    size: int
    fallback_reason: str | None = None
    diagnostic_summary: str | None = None

    @property
    def known(self) -> bool:
        """Return whether a positive manifest size was found."""

        return self.size >= 0


def extract_download_metric(html: str, label: str) -> int:
    """Return one normalized download metric from GitHub package HTML."""

    if not html or not label:
        return -1
    match = re.search(
        re.escape(label) + r"</span>\s*<span[^>]*>([^<]+)",
        html,
        re.DOTALL,
    )
    if match is None:
        return -1
    return parse_metric_value(match.group(1))


def extract_download_metrics(html: str) -> DownloadMetrics:
    """Return all version-page download metrics used by bkg."""

    return DownloadMetrics(
        total=extract_download_metric(html, _DOWNLOAD_LABELS["total"]),
        month=extract_download_metric(html, _DOWNLOAD_LABELS["month"]),
        week=extract_download_metric(html, _DOWNLOAD_LABELS["week"]),
        day=extract_download_metric(html, _DOWNLOAD_LABELS["day"]),
    )


def parse_metric_value(value: str) -> int:
    """Parse GitHub's compact metric text into an integer."""

    normalized = "".join(value.split()).replace(",", "")
    match = _METRIC_PATTERN.fullmatch(normalized)
    if match is None:
        return -1

    suffix = match.group(2).upper()
    multiplier = 1
    if suffix:
        suffix_index = _METRIC_SUFFIXES.find(suffix)
        if suffix_index < 0:
            return -1
        multiplier = 1000 ** (suffix_index + 1)

    try:
        parsed = Decimal(match.group(1)) * multiplier
    except InvalidOperation:
        return -1
    return int(parsed.to_integral_value(rounding=ROUND_HALF_UP))


def extract_embedded_manifest(html: str) -> str:
    """Return the manifest JSON block embedded in GitHub's version page."""

    manifests: list[str] = []
    for block in html.split("</pre>"):
        matches = list(_CODE_PATTERN.finditer(block))
        if matches:
            manifest = block[matches[-1].end() :]
            end = manifest.find("</code>")
            if end >= 0:
                manifest = manifest[:end]
            manifests.append(manifest.replace("&quot;", '"'))
    return "\n".join(manifests)


def manifest_size(manifest: str) -> ManifestSizeResult:
    """Calculate a container manifest size using bkg's current fallback rules."""

    if not manifest:
        return ManifestSizeResult(size=-1)

    try:
        data: object = json.loads(manifest)
    except json.JSONDecodeError:
        return ManifestSizeResult(
            size=-1,
            fallback_reason="malformed JSON",
            diagnostic_summary=_sample_summary(manifest),
        )

    layer_sizes = tuple(_positive_sizes(_array_items(data, "layers")))
    if layer_sizes:
        return ManifestSizeResult(size=math.floor(sum(layer_sizes)))

    manifest_sizes = tuple(_positive_sizes(_array_items(data, "manifests")))
    if manifest_sizes:
        return ManifestSizeResult(
            size=math.floor(sum(manifest_sizes) / len(manifest_sizes))
        )

    return ManifestSizeResult(
        size=-1,
        fallback_reason="unsupported shape",
        diagnostic_summary=_shape_summary(data),
    )


def extract_oci_version_labels(manifest: str) -> tuple[str, ...]:
    """Return non-empty OCI version labels found anywhere in a manifest."""

    if not manifest:
        return ()
    try:
        data: object = json.loads(manifest)
    except json.JSONDecodeError:
        return ()

    labels: list[str] = []
    for value in _walk(data):
        mapping = _as_dict(value)
        if mapping is None:
            continue
        label = mapping.get("org.opencontainers.image.version")
        if label is None or label == "":
            continue
        if isinstance(label, str):
            labels.append(label)
        else:
            labels.append(json.dumps(label, separators=(",", ":")))
    return tuple(labels)


def _walk(value: object) -> Iterator[object]:
    yield value
    mapping = _as_dict(value)
    if mapping is not None:
        for child in mapping.values():
            yield from _walk(child)
        return
    sequence = _as_list(value)
    if sequence is not None:
        for child in sequence:
            yield from _walk(child)


def _array_items(value: object, name: str) -> Iterator[object]:
    for node in _walk(value):
        mapping = _as_dict(node)
        if mapping is None:
            continue
        items = _as_list(mapping.get(name))
        if items is not None:
            yield from items


def _positive_sizes(items: Iterator[object]) -> Iterator[float]:
    for item in items:
        mapping = _as_dict(item)
        if mapping is None:
            continue
        size = mapping.get("size")
        if isinstance(size, bool) or not isinstance(size, int | float):
            continue
        if size > 0:
            yield float(size)


def _sample_summary(manifest: str) -> str:
    return f"sample={json.dumps(manifest[:240])}"


def _shape_summary(data: object) -> str:
    return " ".join(
        (
            _type_summary(data),
            f"mediaTypes={_media_types(data)}",
            f"layerEntries={_array_entry_count(data, 'layers')}",
            f"manifestEntries={_array_entry_count(data, 'manifests')}",
            f"positiveSizeFields={_positive_size_field_count(data)}",
        )
    )


def _type_summary(data: object) -> str:
    mapping = _as_dict(data)
    if mapping is not None:
        return "json_type=object keys=" + ",".join(
            str(key) for key in list(mapping.keys())[:10]
        )
    sequence = _as_list(data)
    if sequence is not None:
        first = sequence[0] if sequence else None
        summary = f"json_type=array length={len(sequence)}"
        first_mapping = _as_dict(first)
        if first_mapping is not None:
            return (
                summary
                + " first_keys="
                + ",".join(str(key) for key in list(first_mapping.keys())[:10])
            )
        return summary + f" first_type={_json_type_name(first)}"
    return f"json_type={_json_type_name(data)}"


def _media_types(data: object) -> str:
    values: set[str] = set()
    for node in _walk(data):
        mapping = _as_dict(node)
        if mapping is None:
            continue
        media_type = mapping.get("mediaType")
        if isinstance(media_type, str):
            values.add(media_type)
    return ",".join(sorted(values)[:4])


def _array_entry_count(data: object, name: str) -> int:
    total = 0
    for node in _walk(data):
        mapping = _as_dict(node)
        if mapping is None:
            continue
        value = _as_list(mapping.get(name))
        if value is not None:
            total += len(value)
    return total


def _positive_size_field_count(data: object) -> int:
    total = 0
    for node in _walk(data):
        mapping = _as_dict(node)
        if mapping is None:
            continue
        size = mapping.get("size")
        if isinstance(size, bool) or not isinstance(size, int | float):
            continue
        if size > 0:
            total += 1
    return total


def _json_type_name(value: object) -> str:
    name = type(value).__name__
    if value is None:
        name = "null"
    elif isinstance(value, bool):
        name = "boolean"
    elif isinstance(value, int | float):
        name = "number"
    elif isinstance(value, str):
        name = "string"
    elif isinstance(value, list):
        name = "array"
    elif isinstance(value, dict):
        name = "object"
    return name


def _as_dict(value: object) -> dict[str, object] | None:
    if isinstance(value, dict):
        return cast(dict[str, object], value)
    return None


def _as_list(value: object) -> list[object] | None:
    if isinstance(value, list):
        return cast(list[object], value)
    return None
