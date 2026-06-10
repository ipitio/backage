"""Tests for atomic durable-output helpers."""

import tempfile
from pathlib import Path

import pytest

from bkg_py.files import atomic_binary_output, atomic_text_output


class TestAtomicOutput:
    """Verify successful replacement and failure cleanup."""

    def test_text_output_replaces_destination_after_success(self) -> None:
        """A complete text write replaces the previous file."""

        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "output.txt"
            destination.write_text("old\n", encoding="utf-8")

            with atomic_text_output(destination) as file:
                file.write("new\n")

            assert destination.read_text(encoding="utf-8") == "new\n"
            assert not list(destination.parent.glob(".output.txt.*"))

    def test_binary_output_preserves_destination_after_failure(self) -> None:
        """An interrupted binary write leaves the previous file intact."""

        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "output.bin"
            destination.write_bytes(b"old")

            def interrupted_write() -> None:
                with atomic_binary_output(destination) as file:
                    file.write(b"partial")
                    raise RuntimeError("interrupted")

            with pytest.raises(RuntimeError, match="interrupted"):
                interrupted_write()

            assert destination.read_bytes() == b"old"
            assert not list(destination.parent.glob(".output.bin.*"))
