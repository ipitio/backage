"""Tests for bounded JSON and XML publication."""

import json
import stat
import tempfile
from pathlib import Path

import pytest

import bkg_py.files
from bkg_py.publication import (
    JsonValue,
    PublicationError,
    PublicationLimits,
    publish_json_file,
    write_xml_file,
    xml_chunks,
)
from bkg_py.publication.promotion import PublicationRecoveryError, is_publication_backup
from bkg_py.runtime import GracefulStop


def _never_stop() -> None:
    pass


class TestPublication:
    """Verify serialization, trimming, limits, and interruption behavior."""

    def test_xml_serialization_preserves_endpoint_shape_and_escaping(self) -> None:
        """Objects, repeated lists, empty values, and scalars retain their shape."""

        value: JsonValue = {
            "text": "A&B<>\"'\t\r\n\u0001",
            "values": [1, True, None],
            "empty_list": [],
            "empty_object": {},
        }

        assert (
            "".join(xml_chunks(value)) == '<?xml version="1.0" encoding="UTF-8"?><xml>'
            "<text>A&amp;B&lt;&gt;&#34;&#39;&#x9;&#xD;\n\ufffd</text>"
            "<values>1</values><values>true</values><values>null</values>"
            "<empty_object></empty_object></xml>"
        )

    def test_root_array_uses_repeated_package_elements(self) -> None:
        """Top-level aggregate arrays remain package-shaped XML."""

        assert (
            "".join(xml_chunks([{"name": "one"}, {"name": "two"}]))
            == '<?xml version="1.0" encoding="UTF-8"?><xml>'
            "<package><name>one</name></package>"
            "<package><name>two</name></package></xml>"
        )

    def test_small_publication_preserves_original_json_bytes(self) -> None:
        """An output below both limits is not needlessly canonicalized."""

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "package.json"
            original = b'{\n  "name": "demo",\n  "tags": ["latest", "edge"]\n}\n'
            source.write_bytes(original)

            result = publish_json_file(source, _never_stop)

            assert not result.trimmed
            assert source.read_bytes() == original
            assert (
                source.with_suffix(".xml").read_text(encoding="utf-8")
                == '<?xml version="1.0" encoding="UTF-8"?><xml>'
                "<name>demo</name><tags>latest</tags><tags>edge</tags></xml>"
            )

    def test_dot_json_aggregate_publishes_dot_xml_endpoint(self) -> None:
        """Hidden aggregate JSON names retain the existing hidden XML name."""

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / ".json"
            source.write_text("[]\n", encoding="utf-8")

            publish_json_file(source, _never_stop)

            assert (Path(directory) / ".xml").is_file()
            assert not (Path(directory) / ".json.xml").exists()

    def test_adaptive_trimming_preserves_protected_versions(self) -> None:
        """Oversized version lists retain latest/newest entries and numeric order."""

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "package.json"
            versions = [
                {
                    "id": identifier,
                    "latest": identifier == 1,
                    "newest": identifier == 6,
                    "tags": ["latest"] if identifier == 1 else [],
                    "notes": "x" * 250,
                }
                for identifier in [6, 1, 5, 2, 4, 3]
            ]
            source.write_text(
                json.dumps(
                    {
                        "package": "demo",
                        "raw_versions": 6,
                        "raw_tagged": 1,
                        "versions": "6",
                        "tagged": "1",
                        "version": versions,
                    }
                ),
                encoding="utf-8",
            )
            limits = PublicationLimits(
                maximum_bytes=1_000,
                hard_maximum_bytes=10_000,
            )

            result = publish_json_file(source, _never_stop, limits)
            published = json.loads(source.read_bytes())
            identifiers = [version["id"] for version in published["version"]]

            assert result.trimmed
            assert result.json_size < limits.maximum_bytes
            assert result.xml_size < limits.maximum_bytes
            assert identifiers == sorted(identifiers)
            assert 1 in identifiers
            assert 6 in identifiers
            assert published["raw_versions"] == len(identifiers)
            assert published["raw_tagged"] == 1
            xml = source.with_suffix(".xml").read_text(encoding="utf-8")
            assert f"<raw_versions>{len(identifiers)}</raw_versions>" in xml
            assert "<raw_tagged>1</raw_tagged>" in xml

    def test_hard_limits_preserve_the_previous_pair(self) -> None:
        """Oversized required data is retryable, never an empty publication."""

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "package.json"
            source.write_text(
                json.dumps({"notes": "x" * 100}),
                encoding="utf-8",
            )

            original = source.read_bytes()
            xml_path = source.with_suffix(".xml")
            xml_path.write_bytes(b"<xml><old>true</old></xml>")
            original_xml = xml_path.read_bytes()

            with pytest.raises(PublicationError, match="hard byte limit"):
                publish_json_file(
                    source,
                    _never_stop,
                    PublicationLimits(maximum_bytes=20, hard_maximum_bytes=20),
                )

            assert source.read_bytes() == original
            assert xml_path.read_bytes() == original_xml

    def test_interruption_preserves_previous_pair_and_cleans_temporary_files(
        self,
    ) -> None:
        """A stop after staging both outputs cannot publish either temporary file."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "package.json"
            xml_path = source.with_suffix(".xml")
            original_json = json.dumps({"notes": "x" * 100}).encode()
            original_xml = b"<xml><old>true</old></xml>"
            source.write_bytes(original_json)
            xml_path.write_bytes(original_xml)

            def stop_after_staging() -> None:
                temporary_paths = [
                    *root.glob(".package.json.*"),
                    *root.glob(".package.xml.*"),
                ]
                if len(temporary_paths) == 2 and all(
                    path.stat().st_size > 0 for path in temporary_paths
                ):
                    raise GracefulStop("test")

            with pytest.raises(GracefulStop):
                publish_json_file(
                    source,
                    stop_after_staging,
                    PublicationLimits(
                        maximum_bytes=10_000,
                        hard_maximum_bytes=20_000,
                    ),
                )

            assert source.read_bytes() == original_json
            assert xml_path.read_bytes() == original_xml
            assert not list(root.glob(".package.json.*"))
            assert not list(root.glob(".package.xml.*"))

    def test_invalid_json_does_not_replace_existing_xml(self) -> None:
        """Malformed input fails without disturbing a previous XML endpoint."""

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "package.json"
            xml_path = source.with_suffix(".xml")
            source.write_text('{"broken":', encoding="utf-8")
            xml_path.write_text("<xml>old</xml>", encoding="utf-8")

            with pytest.raises(PublicationError):
                write_xml_file(source, _never_stop)

            assert xml_path.read_text(encoding="utf-8") == "<xml>old</xml>"


@pytest.mark.parametrize("shape", ["package", "array", "wrapper"])
def test_publication_repairs_represented_counts(tmp_path: Path, shape: str) -> None:
    """Existing count fields follow the selected array in both output formats."""

    package: JsonValue = {
        "raw_versions": 99,
        "raw_tagged": 88,
        "versions": "99",
        "tagged": "88",
        "version": [{"id": 1, "tags": ["latest"]}, {"id": 2, "tags": []}],
    }
    value = (
        [package, package]
        if shape == "array"
        else {"package": [package, package]}
        if shape == "wrapper"
        else package
    )
    source = tmp_path / "package.json"
    source.write_text(json.dumps(value), encoding="utf-8")

    result = publish_json_file(source, _never_stop)

    output = json.loads(source.read_bytes())
    if shape == "array":
        packages = output
    elif shape == "wrapper":
        packages = output["package"]
    else:
        packages = [output]
    assert not result.trimmed
    for published in packages:
        assert published["raw_versions"] == 2
        assert published["raw_tagged"] == 1
        assert published["versions"] == "2"
        assert published["tagged"] == "1"
    xml = source.with_suffix(".xml").read_text(encoding="utf-8")
    assert xml.count("<raw_versions>2</raw_versions>") == len(packages)
    assert xml.count("<raw_tagged>1</raw_tagged>") == len(packages)


def test_xml_repair_also_corrects_json_version_counts(tmp_path: Path) -> None:
    """Count-only corrections promote JSON and XML together, without trimming."""

    source = tmp_path / "package.json"
    source.write_text(
        '{"raw_versions":99,"versions":"99","version":[{"id":5}]}',
        encoding="utf-8",
    )

    write_xml_file(source, _never_stop)

    published = json.loads(source.read_bytes())
    assert published["raw_versions"] == 1
    assert published["versions"] == "1"
    xml = source.with_suffix(".xml").read_text(encoding="utf-8")
    assert "<raw_versions>1</raw_versions>" in xml


@pytest.mark.parametrize("existing", ["both", "json", "xml", "neither"])
@pytest.mark.parametrize("failed_endpoint", ["json", "xml"])
def test_replace_failure_restores_previous_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    existing: str,
    failed_endpoint: str,
) -> None:
    """A late rename failure restores bytes, modes, and originally absent files."""

    source = tmp_path / "source.json"
    source.write_text('{"notes":"new"}', encoding="utf-8")
    destination = tmp_path / "package.json"
    endpoints = {"json": destination, "xml": destination.with_suffix(".xml")}
    previous = {"json": b'{"notes":"old"}', "xml": b"<xml>old</xml>"}
    for extension, path in endpoints.items():
        if existing in ("both", extension):
            path.write_bytes(previous[extension])
            path.chmod(0o640)
    replace = Path.replace

    def fail_replace(path: Path, target: str | Path) -> Path:
        if path.parent == tmp_path and Path(target) == endpoints[failed_endpoint]:
            raise OSError("endpoint replacement failed")
        return replace(path, target)

    monkeypatch.setattr(Path, "replace", fail_replace)

    with pytest.raises(OSError, match="endpoint replacement failed"):
        publish_json_file(source, _never_stop, destination=destination)

    for extension, path in endpoints.items():
        if existing in ("both", extension):
            assert path.read_bytes() == previous[extension]
            assert stat.S_IMODE(path.stat().st_mode) == 0o640
        else:
            assert not path.exists()
    assert sorted(path.name for path in tmp_path.iterdir()) == sorted(
        ["source.json", *(path.name for path in endpoints.values() if path.exists())]
    )


def test_directory_sync_failure_restores_previous_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failure after the first rename also restores both old endpoints."""

    source = tmp_path / "source.json"
    source.write_text('{"notes":"new"}', encoding="utf-8")
    destination = tmp_path / "package.json"
    destination.write_bytes(b'{"notes":"old"}')
    xml_path = destination.with_suffix(".xml")
    xml_path.write_bytes(b"<xml>old</xml>")
    sync = bkg_py.files.sync_directory
    failed = False

    def fail_once(path: Path) -> None:
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("directory sync failed")
        sync(path)

    monkeypatch.setattr(bkg_py.files, "sync_directory", fail_once)

    with pytest.raises(OSError, match="directory sync failed"):
        publish_json_file(source, _never_stop, destination=destination)

    assert destination.read_bytes() == b'{"notes":"old"}'
    assert xml_path.read_bytes() == b"<xml>old</xml>"
    assert not list(tmp_path.glob(".bkg-pair-*"))


def test_failed_restoration_retains_backups_and_raises_fatal_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second filesystem failure cannot destroy the last known good files."""

    source = tmp_path / "source.json"
    source.write_text('{"notes":"new"}', encoding="utf-8")
    destination = tmp_path / "package.json"
    destination.write_bytes(b'{"notes":"old"}')
    xml_path = destination.with_suffix(".xml")
    xml_path.write_bytes(b"<xml>old</xml>")
    replace = Path.replace

    def fail_replace(path: Path, target: str | Path) -> Path:
        if Path(target) == xml_path or path.name.endswith(".restore"):
            raise OSError("filesystem unavailable")
        return replace(path, target)

    monkeypatch.setattr(Path, "replace", fail_replace)

    with pytest.raises(PublicationRecoveryError, match="previous outputs retained"):
        publish_json_file(source, _never_stop, destination=destination)

    backups = [path for path in tmp_path.iterdir() if is_publication_backup(path)]
    assert len(backups) == 1
    assert (backups[0] / "json.previous").read_bytes() == b'{"notes":"old"}'
    assert (backups[0] / "xml.previous").read_bytes() == b"<xml>old</xml>"
    assert not list(tmp_path.glob(".package.json.*"))
    assert not list(tmp_path.glob(".package.xml.*"))


def test_symlink_destination_is_rejected_before_replacing_either_file(
    tmp_path: Path,
) -> None:
    """Publication does not replace symlinks or alter their targets."""

    source = tmp_path / "source.json"
    source.write_text('{"notes":"new"}', encoding="utf-8")
    destination = tmp_path / "package.json"
    destination.write_bytes(b'{"notes":"old"}')
    target = tmp_path / "external.xml"
    target.write_bytes(b"<xml>old</xml>")
    xml_path = destination.with_suffix(".xml")
    xml_path.symlink_to(target)

    with pytest.raises(OSError, match="not a regular file"):
        publish_json_file(source, _never_stop, destination=destination)

    assert destination.read_bytes() == b'{"notes":"old"}'
    assert xml_path.is_symlink()
    assert target.read_bytes() == b"<xml>old</xml>"
    assert not list(tmp_path.glob(".bkg-pair-*"))
