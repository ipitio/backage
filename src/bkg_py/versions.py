"""Version-page parsing and container manifest helpers."""

from __future__ import annotations

import base64
import html as html_lib
import json
import math
import re
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import cast
from urllib.parse import unquote_plus

from .version_selection import VersionCandidate

_DOWNLOAD_LABELS = {
    "total": "Total downloads",
    "month": "Last 30 days",
    "week": "Last week",
    "day": "Today",
}
_METRIC_SUFFIXES = "KMBTPEZY"
_METRIC_PATTERN = re.compile(r"^([0-9]+(?:\.[0-9]+)?)([A-Za-z]?)$")
_CODE_BLOCK_PATTERN = re.compile(r"<code\b[^>]*>(.*?)</code>", re.DOTALL)
_LIST_ITEM_PATTERN = re.compile(
    r"<li\b[^>]*class=\"[^\"]*\bBox-row\b[^\"]*\"[^>]*>(.*?)</li>",
    re.DOTALL,
)
_MANIFEST_HEADING_PATTERN = re.compile(
    r"<h4\b[^>]*>\s*Manifest\s*</h4>",
    re.IGNORECASE | re.DOTALL,
)
_MUTED_SPAN_PATTERN = re.compile(
    r"<span class=\"color-fg-muted\">([^<]+)</span>",
    re.DOTALL,
)
_PRE_BLOCK_PATTERN = re.compile(r"<pre\b[^>]*>(.*?)</pre>", re.DOTALL)
_TAG_PATTERN = re.compile(r"<[^>]+>", re.DOTALL)


@dataclass(frozen=True)
class DownloadMetrics:
    """Download counters extracted from one version page."""

    total: int
    month: int
    week: int
    day: int


@dataclass(frozen=True)
class VersionPageData:
    """Version-page values extracted from one GitHub package version page."""

    metrics: DownloadMetrics
    manifest: str

    def json_object(self) -> dict[str, object]:
        """Return the shell-compatible JSON representation."""

        return {
            "downloads": self.metrics.total,
            "downloads_month": self.metrics.month,
            "downloads_week": self.metrics.week,
            "downloads_day": self.metrics.day,
            "manifest": self.manifest,
        }


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


@dataclass(frozen=True)
class VersionListEntry:
    """One package version advertised on a GitHub version listing page."""

    version_id: int
    name: str
    tags: tuple[str, ...] = ()

    def json_object(self) -> dict[str, object]:
        """Return the shell-compatible JSON representation."""

        return {
            "id": self.version_id,
            "name": self.name,
            "tags": list(self.tags),
        }

    def candidate(self) -> VersionCandidate:
        """Return the entry in the form used by version selection."""

        return VersionCandidate(str(self.version_id), self.name, self.tags)


@dataclass(frozen=True)
class VersionCacheRecord:
    """One shell pipeline cache record for a package version candidate."""

    version_id: str
    source: str
    tags: str

    def tsv_row(self) -> str:
        """Return a shell-readable tab-separated representation."""

        encoded_tags = base64.b64encode(self.tags.encode()).decode()
        return f"{self.version_id}\t{self.source}\t{encoded_tags}"


@dataclass(frozen=True)
class VersionListingContext:
    """Package identity needed to recognize version-listing links."""

    owner_type: str
    owner: str
    repo: str
    package_type: str
    package: str


def package_html_base_path(context: VersionListingContext) -> str:
    """Return GitHub's owner-scoped package path without a leading slash."""

    return (
        f"{context.owner_type}/{context.owner}/packages/"
        f"{context.package_type}/{context.package}"
    )


def package_detail_html_url(context: VersionListingContext) -> str:
    """Return the public package detail page URL."""

    return (
        f"https://github.com/{context.owner_type}/{context.owner}/packages/"
        f"{context.package_type}/package/{context.package}"
    )


def package_versions_html_url(
    context: VersionListingContext,
    page_number: int,
    *,
    tagged: bool = False,
) -> str:
    """Return the public package versions listing URL."""

    query = (
        f"filters%5Bversion_type%5D=tagged&page={page_number}"
        if tagged
        else f"page={page_number}"
    )
    return f"https://github.com/{package_html_base_path(context)}/versions?{query}"


def package_version_detail_html_url(
    context: VersionListingContext,
    version_id: str,
) -> str:
    """Return the public package version detail page URL."""

    return f"https://github.com/{package_html_base_path(context)}/{version_id}"


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


def parse_version_listing_html(
    html: str,
    context: VersionListingContext,
) -> tuple[VersionListEntry, ...]:
    """Parse GitHub's package-version listing HTML into version entries."""

    if not html or not context.package:
        return ()

    owner_prefix = (
        f"{re.escape(context.owner_type)}/{re.escape(context.owner)}/packages/"
        f"{re.escape(context.package_type)}/{re.escape(context.package)}"
    )
    repo_prefix = (
        f"{re.escape(context.owner)}/{re.escape(context.repo)}/pkgs/"
        f"{re.escape(context.package_type)}/{re.escape(context.package)}"
    )
    prefix_pattern = f"(?:{owner_prefix}|{repo_prefix})"
    tag_link_pattern = re.compile(
        rf'href="/{prefix_pattern}/([0-9]+)\?tag=([^"&]+)',
        re.DOTALL,
    )
    version_link_pattern = re.compile(
        rf'href="/{prefix_pattern}/([0-9]+)"',
        re.DOTALL,
    )

    entries: list[VersionListEntry] = []
    for match in _LIST_ITEM_PATTERN.finditer(html):
        entry = _parse_listing_item(
            match.group(1),
            tag_link_pattern=tag_link_pattern,
            version_link_pattern=version_link_pattern,
            prefix_pattern=prefix_pattern,
        )
        if entry is not None:
            entries.append(entry)
    return tuple(entries)


def version_cache_records(json_text: str) -> tuple[VersionCacheRecord, ...]:
    """Return normalized shell cache records for a version page JSON array."""

    records: list[VersionCacheRecord] = []
    for version in _version_values(json_text):
        source_json = json.dumps(
            version,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        records.append(
            VersionCacheRecord(
                version_id=_candidate_id(version),
                source=base64.b64encode(source_json.encode()).decode(),
                tags=_candidate_tags(version),
            )
        )
    return tuple(records)


def version_candidates(json_text: str) -> tuple[VersionCandidate, ...]:
    """Return normalized candidates from a version page JSON array."""

    return version_candidates_from_value(_version_values(json_text))


def version_candidates_from_value(value: object) -> tuple[VersionCandidate, ...]:
    """Return normalized candidates from an already-decoded version array."""

    candidates: list[VersionCandidate] = []
    for version in _as_list(value) or ():
        mapping = _as_dict(version)
        name = _jq_text(mapping.get("name")) if mapping is not None else "null"
        tags = _candidate_tags(version)
        candidates.append(
            VersionCandidate(
                version_id=_candidate_id(version),
                name=name,
                tags=tuple(tags.split(",")) if tags else (),
            )
        )
    return tuple(candidates)


def extract_embedded_manifest(html: str) -> str:
    """Return the manifest JSON block embedded in GitHub's version page."""

    for block in _manifest_candidate_blocks(html):
        for candidate in _manifest_text_candidates(block):
            manifest = _normalize_html_text(candidate)
            if _is_manifest_json(manifest):
                return manifest
    return ""


def extract_version_page_data(html: str) -> VersionPageData:
    """Return all currently migrated values from one version page."""

    return VersionPageData(
        metrics=extract_download_metrics(html),
        manifest=extract_embedded_manifest(html),
    )


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


def _parse_listing_item(
    block: str,
    *,
    tag_link_pattern: re.Pattern[str],
    version_link_pattern: re.Pattern[str],
    prefix_pattern: str,
) -> VersionListEntry | None:
    version_id = ""
    seen_tags: set[str] = set()
    tags: list[str] = []

    for match in tag_link_pattern.finditer(block):
        if not version_id:
            version_id = match.group(1)
        tag = _decode_listing_text(match.group(2))
        if tag and tag not in seen_tags:
            seen_tags.add(tag)
            tags.append(tag)

    if not version_id:
        match = version_link_pattern.search(block)
        if match is not None:
            version_id = match.group(1)

    if not version_id:
        return None

    name = _listing_version_name(block, prefix_pattern, version_id) or version_id
    return VersionListEntry(int(version_id), name, tuple(tags))


def _listing_version_name(block: str, prefix_pattern: str, version_id: str) -> str:
    link_name_pattern = re.compile(
        rf'href="/{prefix_pattern}/{re.escape(version_id)}"[^>]*>([^<]+)</a>',
        re.DOTALL,
    )
    link_name = link_name_pattern.search(block)
    if link_name is not None:
        return _decode_listing_text(link_name.group(1))

    input_value = re.search(r'value="([^"]+)"', block, re.DOTALL)
    if input_value is not None:
        return _decode_listing_text(input_value.group(1))

    muted_span = _MUTED_SPAN_PATTERN.search(block)
    if muted_span is not None:
        candidate = _decode_listing_text(muted_span.group(1))
        if re.match(r"^(?:sha256:|[A-Za-z0-9][^:\s]*)", candidate):
            return candidate
    return ""


def _decode_listing_text(value: str) -> str:
    return unquote_plus(html_lib.unescape(value))


def _candidate_id(value: object) -> str:
    mapping = _as_dict(value)
    if mapping is None:
        return "-1"

    version_id = mapping.get("id")
    if isinstance(version_id, bool):
        return "-1"
    if isinstance(version_id, int):
        return str(version_id)
    if isinstance(version_id, str) and re.fullmatch(r"[0-9]+", version_id):
        return version_id
    return "-1"


def _version_values(json_text: str) -> list[object]:
    try:
        data: object = json.loads(json_text)
    except json.JSONDecodeError:
        return []
    versions = _as_list(data)
    return versions or []


def _candidate_tags(value: object) -> str:
    values: list[str] = []
    for node in _walk(value):
        mapping = _as_dict(node)
        if mapping is None or "tags" not in mapping:
            continue
        tags = mapping["tags"]
        if tags is None:
            continue
        sequence = _as_list(tags)
        if sequence is not None:
            values.append(",".join(_jq_text(item) for item in sequence))
        else:
            values.append(_jq_text(tags))
    return _merge_tag_values(values)


def _merge_tag_values(values: list[str]) -> str:
    merged: list[str] = []
    seen: set[str] = set()
    for value in values:
        for tag in value.split(","):
            normalized = tag.strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            merged.append(normalized)
    return ",".join(merged)


def _jq_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, int | float):
        return str(value)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _manifest_candidate_blocks(html: str) -> Iterator[str]:
    heading = _MANIFEST_HEADING_PATTERN.search(html)
    if heading is not None:
        yield from _pre_blocks(html[heading.end() :])
    yield from _pre_blocks(html)


def _pre_blocks(html: str) -> Iterator[str]:
    for match in _PRE_BLOCK_PATTERN.finditer(html):
        yield match.group(1)


def _manifest_text_candidates(block: str) -> Iterator[str]:
    matches = tuple(_CODE_BLOCK_PATTERN.finditer(block))
    for match in matches:
        yield match.group(1)
    yield block


def _normalize_html_text(value: str) -> str:
    return html_lib.unescape(_TAG_PATTERN.sub("", value)).strip()


def _is_manifest_json(value: str) -> bool:
    if not value:
        return False
    try:
        data: object = json.loads(value)
    except json.JSONDecodeError:
        return False
    mapping = _as_dict(data)
    if mapping is None:
        return False
    return (
        bool(_array_entry_count(data, "layers"))
        or bool(_array_entry_count(data, "manifests"))
        or bool(_media_types(data))
        or "digest" in mapping
    )


def _as_dict(value: object) -> dict[str, object] | None:
    if isinstance(value, dict):
        return cast(dict[str, object], value)
    return None


def _as_list(value: object) -> list[object] | None:
    if isinstance(value, list):
        return cast(list[object], value)
    return None
