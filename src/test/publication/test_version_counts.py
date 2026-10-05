"""Tests for counts derived from represented version identities and tags."""

import pytest

from bkg_py.publication.values import JsonValue
from bkg_py.publication.version_counts import (
    refresh_version_counts,
    version_count_fields,
)


def test_counts_normalize_numeric_ids_and_ignore_placeholder_tags() -> None:
    """Only unique numeric versions and nonempty string tags contribute."""

    versions: list[JsonValue] = [
        {"id": 0, "tags": []},
        {"id": 1, "tags": []},
        {"id": "001", "tags": ["edge"]},
        {"id": "2", "tags": ["", " ", None, False]},
        {"id": True, "tags": ["latest"]},
        {"id": -1, "tags": ["latest"]},
        {"id": "invalid", "tags": ["latest"]},
        {"id": 3.5, "tags": ["latest"]},
        {"id": "9" * 5000},
        {},
        None,
    ]

    assert version_count_fields(versions) == {
        "raw_versions": 3,
        "versions": "3",
        "raw_tagged": 1,
        "tagged": "1",
    }


def test_count_display_uses_shared_decimal_formatting() -> None:
    """Count suffixes match the other human-readable publication metrics."""

    versions: list[JsonValue] = [
        {"id": identifier, "tags": ["release"]} for identifier in range(1000)
    ]

    assert version_count_fields(versions) == {
        "raw_versions": 1000,
        "versions": "1k",
        "raw_tagged": 1000,
        "tagged": "1k",
    }


@pytest.mark.parametrize("versions", [[], [{"id": -1, "tags": ["latest"]}]])
def test_no_numeric_versions_means_zero_not_unknown(versions: list[JsonValue]) -> None:
    """Empty arrays and synthetic versions represent no real version IDs."""

    fields = version_count_fields(versions)

    assert fields["raw_versions"] == 0
    assert fields["raw_tagged"] == 0
    assert fields["versions"] == "0"
    assert fields["tagged"] == "0"


def test_count_repair_removes_only_derived_metric_provenance() -> None:
    """Recomputed counts are not dated observations; source metrics still are."""

    observations: dict[str, JsonValue] = {
        "versions": {"observed_on": "2026-10-03", "stale": True},
        "tagged": {"observed_on": "2026-10-03", "stale": True},
        "downloads": {"observed_on": "2026-10-03", "stale": True},
    }
    package: JsonValue = {
        "raw_versions": 99,
        "versions": "99",
        "raw_tagged": 88,
        "tagged": "88",
        "raw_downloads": 123,
        "date": "2026-10-04",
        "metric_observations": observations,
        "version": [{"id": 1, "tags": ["latest"]}],
    }

    assert refresh_version_counts(package)
    assert package["raw_versions"] == 1
    assert package["raw_tagged"] == 1
    assert package["raw_downloads"] == 123
    assert package["date"] == "2026-10-04"
    assert package["metric_observations"] == {"downloads": observations["downloads"]}
    assert "versions" in observations
    assert not refresh_version_counts(package)


@pytest.mark.parametrize("version", [None, "malformed", {"id": 5}])
def test_malformed_version_array_does_not_reset_known_counts(
    version: JsonValue,
) -> None:
    """Without an array there is no represented count to derive."""

    package: JsonValue = {"raw_versions": 9, "versions": "9", "version": version}

    assert not refresh_version_counts(package)
    assert package["raw_versions"] == 9
    assert package["versions"] == "9"


def test_repair_does_not_add_count_fields_to_generic_json() -> None:
    """Generic converter inputs and their nested records keep their shape."""

    package: JsonValue = {"version": [{"id": 5, "raw_versions": 99}]}

    assert not refresh_version_counts(package)
    assert package == {"version": [{"id": 5, "raw_versions": 99}]}


def test_false_zero_and_stale_count_annotation_are_repaired() -> None:
    """A false placeholder is not an integer count, even when it compares equal."""

    package: JsonValue = {
        "raw_versions": False,
        "metric_observations": {"versions": {"observed_on": None, "stale": True}},
        "version": [],
    }

    assert refresh_version_counts(package)
    assert package["raw_versions"] == 0
    assert package["raw_versions"] is not False
    assert "metric_observations" not in package
    assert "versions" not in package
