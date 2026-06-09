"""Tests for atomic durable-output helpers."""

from pathlib import Path
import tempfile
import unittest

from bkg_py.files import atomic_binary_output, atomic_text_output


class AtomicOutputTests(unittest.TestCase):
    """Verify successful replacement and failure cleanup."""

    def test_text_output_replaces_destination_after_success(self) -> None:
        """A complete text write replaces the previous file."""

        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "output.txt"
            destination.write_text("old\n", encoding="utf-8")

            with atomic_text_output(destination) as file:
                file.write("new\n")

            self.assertEqual(destination.read_text(encoding="utf-8"), "new\n")
            self.assertEqual(list(destination.parent.glob(".output.txt.*")), [])

    def test_binary_output_preserves_destination_after_failure(self) -> None:
        """An interrupted binary write leaves the previous file intact."""

        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "output.bin"
            destination.write_bytes(b"old")

            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                with atomic_binary_output(destination) as file:
                    file.write(b"partial")
                    raise RuntimeError("interrupted")

            self.assertEqual(destination.read_bytes(), b"old")
            self.assertEqual(list(destination.parent.glob(".output.bin.*")), [])


if __name__ == "__main__":
    unittest.main()
