"""Keep last-known published metrics without claiming a fresh observation."""

import json
import math

from .values import JsonValue, PublicationError

_IDENTITY_FIELDS = (
    "owner_id",
    "owner_type",
    "package_type",
    "owner",
    "repo",
    "package",
)
_CASE_INSENSITIVE_FIELDS = frozenset(("owner", "repo"))
_OBSERVATIONS = "metric_observations"
_VERSION_METRICS = (
    "size",
    "downloads",
    "downloads_month",
    "downloads_week",
    "downloads_day",
)
_PACKAGE_METRICS = (*_VERSION_METRICS, "versions", "tagged", "owner_rank", "repo_rank")


def package_identity(value: JsonValue) -> str | None:
    """Return a stable key only for a complete, typed package identity."""

    if not isinstance(value, dict):
        return None
    identity: list[str] = []
    for field in _IDENTITY_FIELDS:
        item = value.get(field)
        if isinstance(item, bool) or not isinstance(item, (str, int)):
            return None
        text = str(item)
        identity.append(text.casefold() if field in _CASE_INSENSITIVE_FIELDS else text)
    return json.dumps(identity, ensure_ascii=False, separators=(",", ":"))


def _known(value: JsonValue) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and value >= 0
        and (not isinstance(value, float) or math.isfinite(value))
    )


def needs_metric_fallback(value: dict[str, JsonValue]) -> bool:
    """Check whether surviving XML might still fill a missing JSON metric."""

    if not value:
        return True
    fields = _PACKAGE_METRICS if package_identity(value) is not None else ()
    if "id" in value:
        fields = _VERSION_METRICS
    if any(not _known(value.get(f"raw_{metric}")) for metric in fields):
        return True
    if any(key.startswith("raw_") and not _known(item) for key, item in value.items()):
        return True
    versions = value.get("version")
    return isinstance(versions, list) and any(
        needs_metric_fallback(version)
        for version in versions
        if isinstance(version, dict)
    )


def _observation(record: dict[str, JsonValue], metric: str) -> JsonValue:
    observations = record.get(_OBSERVATIONS)
    if isinstance(observations, dict):
        previous = observations.get(metric)
        if isinstance(previous, dict):
            date = previous.get("observed_on")
            return {
                "observed_on": date if isinstance(date, str) else None,
                "stale": True,
            }
    date = record.get("date")
    return {"observed_on": date if isinstance(date, str) else None, "stale": True}


def preserve_metrics(
    current: dict[str, JsonValue], previous: dict[str, JsonValue]
) -> dict[str, JsonValue]:
    """Carry missing numeric fields and their display values, never maxima."""

    result = dict(current)
    incoming = current.get(_OBSERVATIONS)
    observations = dict(incoming) if isinstance(incoming, dict) else {}
    for key, value in previous.items():
        if not key.startswith("raw_") or not _known(value) or _known(current.get(key)):
            continue
        metric = key.removeprefix("raw_")
        result[key] = value
        if metric in previous:
            result[metric] = previous[metric]
        observations[metric] = _observation(previous, metric)
    if observations:
        result[_OBSERVATIONS] = observations
    return result


def _version_key(value: JsonValue) -> str | None:
    if not isinstance(value, dict):
        return None
    identifier = value.get("id")
    if isinstance(identifier, bool) or not isinstance(identifier, (str, int)):
        return None
    return str(identifier)


def preserve_package(
    current: dict[str, JsonValue], previous: dict[str, JsonValue]
) -> dict[str, JsonValue]:
    """Merge metrics only for matching packages and selected version identities."""

    identity = package_identity(current)
    if identity is None or identity != package_identity(previous):
        return current
    result = preserve_metrics(current, previous)
    versions = current.get("version")
    old_versions = previous.get("version")
    if not isinstance(old_versions, list):
        return result
    if not isinstance(versions, list):
        raise PublicationError(
            "missing or malformed version list for published package"
        )
    if any(_version_key(version) is None for version in versions):
        raise PublicationError("invalid version identity for published package")
    old_by_id = {
        _version_key(version): version
        for version in old_versions
        if isinstance(version, dict) and _version_key(version) is not None
    }
    selected: list[JsonValue] = []
    for version in versions:
        old = old_by_id.get(_version_key(version))
        selected.append(
            preserve_metrics(version, old)
            if isinstance(version, dict) and isinstance(old, dict)
            else version
        )
    result["version"] = selected
    return result
