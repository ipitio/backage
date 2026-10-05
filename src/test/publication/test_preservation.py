"""Tests for last-known index metrics, freshness, and bounded baseline reads."""

import json
import tempfile
import tracemalloc
from pathlib import Path
from typing import cast

import pytest

from bkg_py.publication import (
    JsonValue,
    PublicationError,
    PublicationLimits,
    publish_json_file,
    write_xml_file,
)
from bkg_py.publication.baseline import publication_baseline
from bkg_py.publication.preservation import preserve_package
from bkg_py.runtime import GracefulStop

_OLD_DATE = "2026-09-29"
_NEW_DATE = "2026-10-03"


def _package(name: str = "demo") -> dict[str, JsonValue]:
    return {
        "owner_id": 42,
        "owner_type": "orgs",
        "package_type": "container",
        "owner": "Example",
        "repo": "Images",
        "package": name,
        "date": _OLD_DATE,
        "raw_downloads": 1500,
        "downloads": "1.5k",
        "raw_downloads_day": 7,
        "downloads_day": "7",
        "raw_size": 123,
        "size": "123",
        "version": [
            {
                "id": 7,
                "date": _OLD_DATE,
                "latest": True,
                "newest": True,
                "raw_downloads": 500,
                "downloads": "500",
            }
        ],
    }


def _unobserved() -> dict[str, JsonValue]:
    value = _package()
    value.update(
        date=_NEW_DATE, raw_downloads=-1, downloads="-1", raw_size=-1, size="-1"
    )
    value["version"] = [
        {
            "id": "7",
            "date": _NEW_DATE,
            "latest": True,
            "newest": True,
            "raw_downloads": -1,
            "downloads": "-1",
        }
    ]
    return value


def _write(path: Path, value: JsonValue) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def test_xml_repair_rolls_back_json_when_xml_replacement_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """XML-based metric repair cannot partially replace its input JSON file."""

    source = tmp_path / "demo.json"
    _write(source, _package())
    publish_json_file(source, lambda: None)
    _write(source, _unobserved())
    original_json = source.read_bytes()
    xml_path = source.with_suffix(".xml")
    original_xml = xml_path.read_bytes()
    replace = Path.replace

    def fail_xml(path: Path, target: str | Path) -> Path:
        if path.parent == tmp_path and Path(target) == xml_path:
            raise OSError("XML replacement failed")
        return replace(path, target)

    monkeypatch.setattr(Path, "replace", fail_xml)

    with pytest.raises(OSError, match="XML replacement failed"):
        write_xml_file(source, lambda: None)

    assert source.read_bytes() == original_json
    assert xml_path.read_bytes() == original_xml
    assert sorted(path.name for path in tmp_path.iterdir()) == ["demo.json", "demo.xml"]


@pytest.mark.parametrize("missing", [-1, None, False, "-1"])
def test_unknown_metric_preserves_value_display_and_observation(
    missing: JsonValue,
) -> None:
    """Unobserved fields carry their previous value without changing freshness."""

    current = _unobserved()
    current["raw_downloads"] = missing
    result = preserve_package(current, _package())

    assert result["date"] == _NEW_DATE
    assert result["raw_downloads"] == 1500
    assert result["downloads"] == "1.5k"
    assert result["metric_observations"] == {
        "downloads": {"observed_on": _OLD_DATE, "stale": True},
        "size": {"observed_on": _OLD_DATE, "stale": True},
    }
    versions = result["version"]
    assert isinstance(versions, list)
    assert isinstance(versions[0], dict)
    assert versions[0]["raw_downloads"] == 500
    assert versions[0]["metric_observations"] == {
        "downloads": {"observed_on": _OLD_DATE, "stale": True}
    }


def test_repeated_failure_and_missing_field_do_not_advance_observation_date() -> None:
    """Repeated publication and rendering preserve the original observation."""

    first = preserve_package(_unobserved(), _package())
    current = _unobserved()
    current.pop("raw_downloads")
    current.pop("downloads")
    current["date"] = "2026-10-04"
    second = preserve_package(current, first)

    assert second["metric_observations"] == first["metric_observations"]
    assert second["raw_downloads"] == 1500
    assert preserve_package(second, _package()) == second


@pytest.mark.parametrize("observed", [0, 25])
def test_fresh_zero_and_decrease_replace_old_metrics_and_clear_stale_state(
    observed: int,
) -> None:
    """Preservation is not a historical maximum or a refusal to accept zero."""

    previous = preserve_package(_unobserved(), _package())
    current = _package()
    current.update(date=_NEW_DATE, raw_downloads=observed, downloads=str(observed))
    result = preserve_package(current, previous)

    assert result["raw_downloads"] == observed
    assert "metric_observations" not in result


@pytest.mark.parametrize("field", ["owner_id", "package_type", "package", "repo"])
def test_different_identity_never_inherits_metrics(field: str) -> None:
    """A file path alone cannot justify sharing data between packages."""

    current = _unobserved()
    current[field] = "different"

    assert preserve_package(current, _package()) == current


def test_version_selection_is_retained_and_new_versions_do_not_inherit_counters() -> (
    None
):
    """Only matching selected versions are merged; omitted versions stay omitted."""

    current = _unobserved()
    current["version"] = [{"id": 8, "raw_downloads": -1}]
    result = preserve_package(current, _package())

    assert result["version"] == current["version"]


def test_missing_historical_date_is_reported_as_unknown_not_today() -> None:
    """Legacy values with no observation date do not acquire a fabricated one."""

    previous = _package()
    previous.pop("date")
    result = preserve_package(_unobserved(), previous)

    assert result["metric_observations"] == {
        "downloads": {"observed_on": None, "stale": True},
        "size": {"observed_on": None, "stale": True},
    }


@pytest.mark.parametrize(
    "baseline", ["json", "xml", "in-place", "xml-only", "json-unknown", "json-empty"]
)
def test_pair_publication_preserves_metrics_in_every_entrypoint(
    tmp_path: Path, baseline: str
) -> None:
    """A missing JSON endpoint or direct XML conversion cannot erase known XML."""

    destination = tmp_path / "demo.json"
    _write(destination, _package())
    publish_json_file(destination, lambda: None)
    source = tmp_path / "source.json"
    _write(source, _unobserved())
    if baseline == "xml":
        destination.unlink()
    if baseline == "json-unknown":
        _write(destination, _unobserved())
    if baseline == "json-empty":
        _write(destination, {})
    if baseline in ("in-place", "xml-only"):
        destination.write_bytes(source.read_bytes())
        source = destination
    if baseline == "xml-only":
        write_xml_file(source, lambda: None)
    else:
        publish_json_file(source, lambda: None, destination=destination)

    published: object = json.loads(destination.read_bytes())
    assert isinstance(published, dict)
    assert published["raw_downloads"] == 1500
    assert published["raw_size"] == 123
    xml = destination.with_suffix(".xml").read_text(encoding="utf-8")
    assert "<raw_downloads>1500</raw_downloads>" in xml
    assert f"<observed_on>{_OLD_DATE}</observed_on><stale>true</stale>" in xml


def test_aggregate_merge_is_identity_scoped_and_keeps_authoritative_membership(
    tmp_path: Path,
) -> None:
    """A removed package stays removed and a new package stays unobserved."""

    destination = tmp_path / ".json"
    _write(destination, [_package(), _package("removed")])
    source = tmp_path / "source.json"
    new = _unobserved()
    new["package"] = "new"
    _write(source, [new, _unobserved()])

    publish_json_file(source, lambda: None, destination=destination)
    published = cast(list[dict[str, JsonValue]], json.loads(destination.read_bytes()))
    assert [value["package"] for value in published] == ["new", "demo"]
    assert published[0]["raw_downloads"] == -1
    assert published[1]["raw_downloads"] == 1500


def test_null_observation_date_survives_xml_recovery(tmp_path: Path) -> None:
    """An explicit legacy null date cannot become the text date 'null'."""

    destination = tmp_path / "demo.json"
    previous = _package()
    previous["date"] = None
    _write(destination, previous)
    publish_json_file(destination, lambda: None)
    destination.unlink()
    source = tmp_path / "source.json"
    _write(source, _unobserved())

    publish_json_file(source, lambda: None, destination=destination)

    published = json.loads(destination.read_bytes())
    assert published["metric_observations"]["downloads"] == {
        "observed_on": None,
        "stale": True,
    }


@pytest.mark.parametrize("versions", [None, [{"raw_downloads": -1}]])
def test_malformed_versions_cannot_erase_published_version_metrics(
    tmp_path: Path, versions: JsonValue
) -> None:
    """A malformed version list is not an intentional bounded selection."""

    destination = tmp_path / "demo.json"
    _write(destination, _package())
    original = destination.read_bytes()
    source = tmp_path / "source.json"
    current = _unobserved()
    current["version"] = versions
    _write(source, current)

    with pytest.raises(PublicationError, match="version"):
        publish_json_file(source, lambda: None, destination=destination)

    assert destination.read_bytes() == original


@pytest.mark.parametrize("candidate", [{}, {"raw_downloads": -1}])
def test_incomplete_package_identity_cannot_erase_a_published_package(
    tmp_path: Path, candidate: JsonValue
) -> None:
    """Malformed rendering must fail closed rather than publish an empty object."""

    destination = tmp_path / "demo.json"
    _write(destination, _package())
    original = destination.read_bytes()
    source = tmp_path / "source.json"
    _write(source, candidate)

    with pytest.raises(PublicationError, match="incomplete identity"):
        publish_json_file(source, lambda: None, destination=destination)

    assert destination.read_bytes() == original


def test_size_limits_never_drop_packages_or_required_metrics(tmp_path: Path) -> None:
    """If required content exceeds the hard cap, keep the previous endpoints."""

    destination = tmp_path / ".json"
    _write(destination, [_package()])
    publish_json_file(destination, lambda: None)
    previous_json = destination.read_bytes()
    previous_xml = destination.with_name(".xml").read_bytes()
    source = tmp_path / "source.json"
    _write(source, [_unobserved(), _package("another")])

    with pytest.raises(PublicationError, match="hard byte limit"):
        publish_json_file(
            source, lambda: None, PublicationLimits(100, 100), destination
        )

    assert destination.read_bytes() == previous_json
    assert destination.with_name(".xml").read_bytes() == previous_xml


def test_legacy_untyped_known_metrics_are_not_silently_erased(tmp_path: Path) -> None:
    """Ambiguous identity needs repair, not guessed fallback or missing output."""

    destination = tmp_path / "demo.json"
    previous = _package()
    previous.pop("package_type")
    _write(destination, previous)
    original = destination.read_bytes()
    source = tmp_path / "source.json"
    _write(source, _unobserved())

    with pytest.raises(PublicationError, match="previous endpoint with incomplete"):
        publish_json_file(source, lambda: None, destination=destination)

    assert destination.read_bytes() == original


@pytest.mark.parametrize(
    "corruption", ["truncated-json", "trailing-json", "xml-entity"]
)
def test_unreadable_baseline_cannot_be_overwritten(
    tmp_path: Path, corruption: str
) -> None:
    """Parsing failures and unsafe XML keep the old endpoint available for recovery."""

    destination = tmp_path / "demo.json"
    xml_path = destination.with_suffix(".xml")
    _write(destination, _package())
    if corruption == "truncated-json":
        destination.write_bytes(destination.read_bytes()[:-1])
    elif corruption == "trailing-json":
        destination.write_bytes(destination.read_bytes() + b" unexpected")
    else:
        destination.unlink()
        xml_path.write_bytes(
            b'<!DOCTYPE xml [<!ENTITY value "bad">]><xml>&value;</xml>'
        )
    path = xml_path if corruption == "xml-entity" else destination
    original = path.read_bytes()
    source = tmp_path / "source.json"
    _write(source, _unobserved())

    with pytest.raises(PublicationError, match="cannot read publication baseline"):
        publish_json_file(source, lambda: None, destination=destination)

    assert path.read_bytes() == original


def test_large_baseline_has_bounded_memory_and_cleans_up_after_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Baseline indexing is streaming and never leaves durable cache tables."""

    destination = tmp_path / "owner.json"
    with destination.open("w", encoding="utf-8") as output:
        output.write("[")
        for number in range(2000):
            if number:
                output.write(",")
            value = _package(str(number))
            value["padding"] = "x" * 2000
            json.dump(value, output)
        output.write("]")
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    tracemalloc.start()
    try:
        with publication_baseline(destination, lambda: None) as baseline:
            current = _unobserved()
            current["package"] = "1999"
            result = cast(dict[str, JsonValue], baseline.preserve(current))
            assert result["raw_downloads"] == 1500
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 3 * 1024 * 1024
    assert not list(tmp_path.glob("bkg-publication-*"))

    def stop() -> None:
        raise GracefulStop("baseline test")

    with (
        pytest.raises(GracefulStop, match="baseline test"),
        publication_baseline(destination, stop),
    ):
        pytest.fail("stopped baseline should not be yielded")
    assert not list(tmp_path.glob("bkg-publication-*"))
