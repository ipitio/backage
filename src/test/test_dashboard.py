"""Tests for bounded dashboard artifact publication."""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from bkg_py.dashboard import (
    DASHBOARD_HISTORY_RETENTION_DAYS,
    publish_dashboard,
)
from bkg_py.database import (
    DashboardDistributionItem,
    DashboardFreshnessBucket,
    DashboardMetricCoverage,
    DashboardProjection,
    PackageInventory,
)

TODAY = "2026-08-10"


def _projection(*, packages: int = 5) -> DashboardProjection:
    return DashboardProjection(
        inventory=PackageInventory(2, 4, packages),
        resolved_packages=max(0, packages - 1),
        package_types=(
            DashboardDistributionItem("container", 3),
            DashboardDistributionItem("npm", 1),
        ),
        other_packages=max(0, packages - 4),
        freshness=(
            DashboardFreshnessBucket("today", 2),
            DashboardFreshnessBucket("days_1_7", 1),
            DashboardFreshnessBucket("days_8_30", 1),
            DashboardFreshnessBucket("days_31_plus", 0),
            DashboardFreshnessBucket("unknown", max(0, packages - 4)),
        ),
        metrics=(
            DashboardMetricCoverage("size", "bytes", 4, 1_024),
            DashboardMetricCoverage("downloads_total", "downloads", 3, 900),
            DashboardMetricCoverage("downloads_month", "downloads", 3, 90),
            DashboardMetricCoverage("downloads_week", "downloads", 3, 21),
            DashboardMetricCoverage("downloads_day", "downloads", 3, 3),
        ),
    )


def _sample(sample_date: date, packages: int) -> dict[str, object]:
    known_packages = min(1, packages)
    return {
        "date": sample_date.isoformat(),
        "owners": min(1, packages),
        "repositories": min(2, packages),
        "packages": packages,
        "size_known_packages": known_packages,
        "downloads_known_packages": known_packages,
    }


def test_dashboard_serializes_explicit_units_coverage_and_bounded_history(
    tmp_path: Path,
) -> None:
    """Publication replaces today's sample and keeps only the defined window."""

    current_date = date.fromisoformat(TODAY)
    prior_samples = [
        _sample(current_date - timedelta(days=offset), offset) for offset in range(201)
    ]
    (tmp_path / "dashboard-history.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "retention_days": DASHBOARD_HISTORY_RETENTION_DAYS,
                "samples": prior_samples,
            }
        ),
        encoding="utf-8",
    )

    result = publish_dashboard(_projection(), tmp_path, TODAY, lambda: None)

    dashboard = json.loads((tmp_path / "dashboard.json").read_text(encoding="utf-8"))
    history = json.loads(
        (tmp_path / "dashboard-history.json").read_text(encoding="utf-8")
    )
    assert dashboard["schema_version"] == 1
    assert dashboard["generated_date"] == TODAY
    assert dashboard["inventory"] == {
        "owners": 2,
        "repositories": 4,
        "packages": 5,
        "resolved_packages": 4,
    }
    assert dashboard["package_types"]["items"][0] == {
        "name": "container",
        "packages": 3,
        "coverage_basis_points": 6_000,
    }
    assert dashboard["package_types"]["other_packages"] == 1
    assert dashboard["package_types"]["other_coverage_basis_points"] == 2_000
    assert dashboard["metrics"]["size"] == {
        "unit": "bytes",
        "denominator": "catalog_packages",
        "unknown_treatment": "negative_or_missing_current_value",
        "known_packages": 4,
        "unknown_packages": 1,
        "coverage_basis_points": 8_000,
        "value": 1_024,
    }
    assert not result.history_reset
    assert result.history_samples == DASHBOARD_HISTORY_RETENTION_DAYS
    assert len(history["samples"]) == DASHBOARD_HISTORY_RETENTION_DAYS
    assert (
        history["samples"][0]["date"]
        == (
            current_date - timedelta(days=DASHBOARD_HISTORY_RETENTION_DAYS - 1)
        ).isoformat()
    )
    assert history["samples"][-1] == {
        "date": TODAY,
        "owners": 2,
        "repositories": 4,
        "packages": 5,
        "size_known_packages": 4,
        "downloads_known_packages": 3,
    }
    assert result.dashboard_bytes == (tmp_path / "dashboard.json").stat().st_size
    assert result.history_bytes == (tmp_path / "dashboard-history.json").stat().st_size


def test_dashboard_recovers_from_invalid_prior_history(tmp_path: Path) -> None:
    """An invalid bounded artifact starts a truthful history at the current day."""

    (tmp_path / "dashboard-history.json").write_text(
        '{"schema_version":99,"samples":[]}',
        encoding="utf-8",
    )

    result = publish_dashboard(_projection(), tmp_path, TODAY, lambda: None)

    history = json.loads(
        (tmp_path / "dashboard-history.json").read_text(encoding="utf-8")
    )
    assert result.history_reset
    assert [sample["date"] for sample in history["samples"]] == [TODAY]


def test_dashboard_recovers_from_inconsistent_prior_history(tmp_path: Path) -> None:
    """A structurally valid but impossible sample cannot enter the new history."""

    prior = _sample(date.fromisoformat(TODAY) - timedelta(days=1), 1)
    prior["size_known_packages"] = 2
    (tmp_path / "dashboard-history.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "retention_days": DASHBOARD_HISTORY_RETENTION_DAYS,
                "samples": [prior],
            }
        ),
        encoding="utf-8",
    )

    result = publish_dashboard(_projection(), tmp_path, TODAY, lambda: None)

    history = json.loads(
        (tmp_path / "dashboard-history.json").read_text(encoding="utf-8")
    )
    assert result.history_reset
    assert [sample["date"] for sample in history["samples"]] == [TODAY]


def test_dashboard_stop_preserves_both_prior_artifacts(tmp_path: Path) -> None:
    """A graceful stop is observed before either generated file is replaced."""

    dashboard_path = tmp_path / "dashboard.json"
    history_path = tmp_path / "dashboard-history.json"
    dashboard_path.write_bytes(b"prior dashboard\n")
    history_path.write_bytes(b"prior history\n")

    def stop() -> None:
        raise RuntimeError("stop requested")

    with pytest.raises(RuntimeError, match="stop requested"):
        publish_dashboard(_projection(), tmp_path, TODAY, stop)

    assert dashboard_path.read_bytes() == b"prior dashboard\n"
    assert history_path.read_bytes() == b"prior history\n"
