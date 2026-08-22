"""Serialize bounded dashboard projections and daily trend history."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import cast

from ..database.dashboard import (
    PACKAGE_TYPE_LIMIT,
    DashboardMetricCoverage,
    DashboardProjection,
)
from ..files import atomic_binary_output

DASHBOARD_SCHEMA_VERSION = 1
DASHBOARD_HISTORY_SCHEMA_VERSION = 1
DASHBOARD_HISTORY_RETENTION_DAYS = 180
DASHBOARD_FILE = "dashboard.json"
DASHBOARD_HISTORY_FILE = "dashboard-history.json"
_HISTORY_MAX_BYTES = 1_000_000
_HISTORY_SAMPLE_FIELDS = frozenset(
    {
        "date",
        "owners",
        "repositories",
        "packages",
        "size_known_packages",
        "downloads_known_packages",
    }
)


@dataclass(frozen=True)
class DashboardPublicationResult:
    """Sizes and history recovery state for one dashboard publication."""

    dashboard_bytes: int
    history_bytes: int
    history_samples: int
    history_reset: bool


def publish_dashboard(
    projection: DashboardProjection,
    destination: Path,
    today: str,
    check_stop: Callable[[], None],
) -> DashboardPublicationResult:
    """Atomically publish current data and one bounded daily history sample."""

    current_date = date.fromisoformat(today)
    if current_date.isoformat() != today:
        raise ValueError("dashboard date must use YYYY-MM-DD")
    destination.mkdir(parents=True, exist_ok=True)
    history_path = destination / DASHBOARD_HISTORY_FILE
    prior_samples, history_reset = _load_history(history_path, current_date)
    current_sample = _history_sample(projection, today)
    samples_by_date = {
        str(sample["date"]): sample for sample in (*prior_samples, current_sample)
    }
    cutoff = current_date - timedelta(days=DASHBOARD_HISTORY_RETENTION_DAYS - 1)
    samples = tuple(
        samples_by_date[key]
        for key in sorted(samples_by_date)
        if cutoff <= date.fromisoformat(key) <= current_date
    )[-DASHBOARD_HISTORY_RETENTION_DAYS:]
    dashboard_output = _json_bytes(_dashboard_document(projection, today))
    history_output = _json_bytes(
        {
            "schema_version": DASHBOARD_HISTORY_SCHEMA_VERSION,
            "retention_days": DASHBOARD_HISTORY_RETENTION_DAYS,
            "samples": samples,
        }
    )

    check_stop()
    _write_json(history_path, history_output)
    _write_json(destination / DASHBOARD_FILE, dashboard_output)
    return DashboardPublicationResult(
        len(dashboard_output),
        len(history_output),
        len(samples),
        history_reset,
    )


def _dashboard_document(
    projection: DashboardProjection,
    today: str,
) -> dict[str, object]:
    inventory = projection.inventory
    total = inventory.packages
    return {
        "schema_version": DASHBOARD_SCHEMA_VERSION,
        "generated_date": today,
        "inventory": {
            "owners": inventory.owners,
            "repositories": inventory.repositories,
            "packages": total,
            "resolved_packages": projection.resolved_packages,
        },
        "package_types": {
            "unit": "packages",
            "denominator": "catalog_packages",
            "limit": PACKAGE_TYPE_LIMIT,
            "items": [
                {
                    "name": item.name,
                    "packages": item.packages,
                    "coverage_basis_points": _basis_points(item.packages, total),
                }
                for item in projection.package_types
            ],
            "other_packages": projection.other_packages,
            "other_coverage_basis_points": _basis_points(
                projection.other_packages,
                total,
            ),
        },
        "freshness": {
            "unit": "packages",
            "denominator": "catalog_packages",
            "unknown_treatment": "missing_invalid_or_future_observed_date",
            "buckets": [
                {
                    "name": bucket.name,
                    "packages": bucket.packages,
                    "coverage_basis_points": _basis_points(bucket.packages, total),
                }
                for bucket in projection.freshness
            ],
        },
        "metrics": {
            metric.name: _metric_document(metric, total)
            for metric in projection.metrics
        },
        "history": {
            "path": DASHBOARD_HISTORY_FILE,
            "schema_version": DASHBOARD_HISTORY_SCHEMA_VERSION,
            "retention_days": DASHBOARD_HISTORY_RETENTION_DAYS,
        },
    }


def _metric_document(
    metric: DashboardMetricCoverage,
    total_packages: int,
) -> dict[str, object]:
    return {
        "unit": metric.unit,
        "denominator": "catalog_packages",
        "unknown_treatment": "negative_or_missing_current_value",
        "known_packages": metric.known_packages,
        "unknown_packages": max(0, total_packages - metric.known_packages),
        "coverage_basis_points": _basis_points(
            metric.known_packages,
            total_packages,
        ),
        "value": metric.value,
    }


def _history_sample(
    projection: DashboardProjection,
    today: str,
) -> dict[str, object]:
    metrics = {metric.name: metric for metric in projection.metrics}
    return {
        "date": today,
        "owners": projection.inventory.owners,
        "repositories": projection.inventory.repositories,
        "packages": projection.inventory.packages,
        "size_known_packages": metrics["size"].known_packages,
        "downloads_known_packages": metrics["downloads_total"].known_packages,
    }


def _load_history(
    path: Path,
    current_date: date,
) -> tuple[tuple[dict[str, object], ...], bool]:
    value, reset = _read_history_value(path)
    if value is None:
        return (), reset
    document = _object_mapping(value)
    if document is None:
        return (), True
    if (
        document.get("schema_version") != DASHBOARD_HISTORY_SCHEMA_VERSION
        or document.get("retention_days") != DASHBOARD_HISTORY_RETENTION_DAYS
    ):
        return (), True
    raw_samples = document.get("samples")
    if not isinstance(raw_samples, list):
        return (), True

    samples: list[dict[str, object]] = []
    seen_dates: set[str] = set()
    for sample in cast(list[object], raw_samples):
        validated = _validated_history_sample(sample, current_date)
        if validated is None or str(validated["date"]) in seen_dates:
            return (), True
        seen_dates.add(str(validated["date"]))
        samples.append(validated)
    return tuple(samples), False


def _read_history_value(path: Path) -> tuple[object | None, bool]:
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return None, False
    if size > _HISTORY_MAX_BYTES:
        return None, True
    try:
        value: object = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, False
    except UnicodeDecodeError, json.JSONDecodeError:
        return None, True
    return value, value is None


def _validated_history_sample(
    value: object,
    current_date: date,
) -> dict[str, object] | None:
    sample = _object_mapping(value)
    if sample is None or frozenset(sample) != _HISTORY_SAMPLE_FIELDS:
        return None
    sample_date = sample.get("date")
    parsed_date = _history_date(sample_date)
    if parsed_date is None or parsed_date > current_date:
        return None
    for field in _HISTORY_SAMPLE_FIELDS - {"date"}:
        field_value = sample.get(field)
        if (
            isinstance(field_value, bool)
            or not isinstance(field_value, int)
            or field_value < 0
        ):
            return None
    packages = cast(int, sample["packages"])
    if (
        cast(int, sample["owners"]) > cast(int, sample["repositories"])
        or cast(int, sample["repositories"]) > packages
        or cast(int, sample["size_known_packages"]) > packages
        or cast(int, sample["downloads_known_packages"]) > packages
    ):
        return None
    return dict(sample)


def _history_date(value: object) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _object_mapping(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    mapping = cast(dict[object, object], value)
    if not all(isinstance(key, str) for key in mapping):
        return None
    return cast(dict[str, object], mapping)


def _basis_points(value: int, denominator: int) -> int:
    if denominator <= 0:
        return 0
    return min(10_000, max(0, value) * 10_000 // denominator)


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, separators=(",", ":"), sort_keys=True) + "\n").encode()


def _write_json(path: Path, content: bytes) -> None:
    with atomic_binary_output(path) as output:
        output.write(content)
