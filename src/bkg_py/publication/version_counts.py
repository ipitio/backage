"""Derive version counts from the array represented in an endpoint."""

from collections.abc import Sequence

from .formatting import human_metric
from .values import JsonValue

_COUNT_METRICS = ("versions", "tagged")


def version_count_fields(versions: Sequence[JsonValue]) -> dict[str, JsonValue]:
    """Count unique numeric IDs and their nonempty tags, excluding placeholders."""

    identifiers: set[int] = set()
    tagged: set[int] = set()
    for version in versions:
        if not isinstance(version, dict):
            continue
        identifier = _numeric_identifier(version.get("id"))
        if identifier is None:
            continue
        identifiers.add(identifier)
        tags = version.get("tags")
        if isinstance(tags, list) and any(
            isinstance(tag, str) and tag.strip() for tag in tags
        ):
            tagged.add(identifier)
    fields: dict[str, JsonValue] = {}
    for metric, count in zip(
        _COUNT_METRICS, (len(identifiers), len(tagged)), strict=True
    ):
        fields[f"raw_{metric}"] = count
        fields[metric] = human_metric(count)
    return fields


def refresh_version_counts(value: JsonValue) -> bool:
    """Update existing count fields in package and aggregate endpoint shapes."""

    if isinstance(value, list):
        packages = value
    elif isinstance(value, dict):
        nested = value.get("package")
        packages = nested if isinstance(nested, list) else [value]
    else:
        return False
    changed = False
    for package in packages:
        changed = _refresh_package_counts(package) or changed
    return changed


def _refresh_package_counts(value: JsonValue) -> bool:
    if not isinstance(value, dict):
        return False
    versions = value.get("version")
    if not isinstance(versions, list) or not any(
        metric in value or f"raw_{metric}" in value for metric in _COUNT_METRICS
    ):
        return False
    fields = version_count_fields(versions)
    changed = False
    for field, count in fields.items():
        if field in value and (
            value[field] != count or type(value[field]) is not type(count)
        ):
            value[field] = count
            changed = True
    observations = value.get("metric_observations")
    if isinstance(observations, dict) and any(
        metric in observations for metric in _COUNT_METRICS
    ):
        retained = {
            metric: observation
            for metric, observation in observations.items()
            if metric not in _COUNT_METRICS
        }
        if retained:
            value["metric_observations"] = retained
        else:
            del value["metric_observations"]
        changed = True
    return changed


def _numeric_identifier(value: JsonValue) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str) and value.isdecimal():
        try:
            return int(value)
        except ValueError:
            return None
    return None
