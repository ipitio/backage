"""Tests for bounded JSON and XML publication."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from bkg_py.publication import (
    PublicationError,
    PublicationLimits,
    publish_json_file,
    write_xml_file,
    xml_chunks,
)
from bkg_py.runtime import GracefulStop


def _never_stop() -> None:
    pass


class PublicationTests(unittest.TestCase):
    """Verify serialization, trimming, limits, and interruption behavior."""

    def test_xml_serialization_preserves_endpoint_shape_and_escaping(self) -> None:
        """Objects, repeated lists, empty values, and scalars retain their shape."""

        value = {
            "text": "A&B<>\"'\t\r\n\u0001",
            "values": [1, True, None],
            "empty_list": [],
            "empty_object": {},
        }

        self.assertEqual(
            "".join(xml_chunks(value)),
            '<?xml version="1.0" encoding="UTF-8"?><xml>'
            "<text>A&amp;B&lt;&gt;&#34;&#39;&#x9;&#xD;\n\ufffd</text>"
            "<values>1</values><values>true</values><values>null</values>"
            "<empty_object></empty_object></xml>",
        )

    def test_root_array_uses_repeated_package_elements(self) -> None:
        """Top-level aggregate arrays remain package-shaped XML."""

        self.assertEqual(
            "".join(xml_chunks([{"name": "one"}, {"name": "two"}])),
            '<?xml version="1.0" encoding="UTF-8"?><xml>'
            "<package><name>one</name></package>"
            "<package><name>two</name></package></xml>",
        )

    def test_small_publication_preserves_original_json_bytes(self) -> None:
        """An output below both limits is not needlessly canonicalized."""

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "package.json"
            original = b'{\n  "name": "demo",\n  "tags": ["latest", "edge"]\n}\n'
            source.write_bytes(original)

            result = publish_json_file(source, _never_stop)

            self.assertFalse(result.trimmed)
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(
                source.with_suffix(".xml").read_text(encoding="utf-8"),
                '<?xml version="1.0" encoding="UTF-8"?><xml>'
                "<name>demo</name><tags>latest</tags><tags>edge</tags></xml>",
            )

    def test_dot_json_aggregate_publishes_dot_xml_endpoint(self) -> None:
        """Hidden aggregate JSON names retain the existing hidden XML name."""

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / ".json"
            source.write_text("[]\n", encoding="utf-8")

            publish_json_file(source, _never_stop)

            self.assertTrue((Path(directory) / ".xml").is_file())
            self.assertFalse((Path(directory) / ".json.xml").exists())

    def test_adaptive_trimming_preserves_protected_versions(self) -> None:
        """Oversized version lists retain latest/newest entries and numeric order."""

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "package.json"
            versions = [
                {
                    "id": identifier,
                    "latest": identifier == 1,
                    "newest": identifier == 6,
                    "notes": "x" * 250,
                }
                for identifier in [6, 1, 5, 2, 4, 3]
            ]
            source.write_text(
                json.dumps({"package": "demo", "version": versions}),
                encoding="utf-8",
            )
            limits = PublicationLimits(
                maximum_bytes=750,
                hard_maximum_bytes=10_000,
            )

            result = publish_json_file(source, _never_stop, limits)
            published = json.loads(source.read_bytes())
            identifiers = [version["id"] for version in published["version"]]

            self.assertTrue(result.trimmed)
            self.assertLess(result.json_size, limits.maximum_bytes)
            self.assertLess(result.xml_size, limits.maximum_bytes)
            self.assertEqual(identifiers, sorted(identifiers))
            self.assertIn(1, identifiers)
            self.assertIn(6, identifiers)

    def test_hard_limits_replace_independently_oversized_formats(self) -> None:
        """Hard caps always leave valid minimal JSON and XML endpoints."""

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "package.json"
            source.write_text(
                json.dumps({"notes": "x" * 100}),
                encoding="utf-8",
            )

            result = publish_json_file(
                source,
                _never_stop,
                PublicationLimits(
                    maximum_bytes=10_000,
                    hard_maximum_bytes=20,
                ),
            )

            self.assertEqual(source.read_bytes(), b"{}\n")
            empty_xml = b'<?xml version="1.0" encoding="UTF-8"?><xml></xml>\n'
            self.assertEqual(source.with_suffix(".xml").read_bytes(), empty_xml)
            self.assertEqual(result.json_size, 3)
            self.assertEqual(result.xml_size, len(empty_xml))

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

            with self.assertRaises(GracefulStop):
                publish_json_file(
                    source,
                    stop_after_staging,
                    PublicationLimits(
                        maximum_bytes=10_000,
                        hard_maximum_bytes=20,
                    ),
                )

            self.assertEqual(source.read_bytes(), original_json)
            self.assertEqual(xml_path.read_bytes(), original_xml)
            self.assertEqual(list(root.glob(".package.json.*")), [])
            self.assertEqual(list(root.glob(".package.xml.*")), [])

    def test_invalid_json_does_not_replace_existing_xml(self) -> None:
        """Malformed input fails without disturbing a previous XML endpoint."""

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "package.json"
            xml_path = source.with_suffix(".xml")
            source.write_text('{"broken":', encoding="utf-8")
            xml_path.write_text("<xml>old</xml>", encoding="utf-8")

            with self.assertRaises(PublicationError):
                write_xml_file(source, _never_stop)

            self.assertEqual(xml_path.read_text(encoding="utf-8"), "<xml>old</xml>")


if __name__ == "__main__":
    unittest.main()
