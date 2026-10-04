"""Tests for read-only generated-file validation through the public CLI."""

from pathlib import Path

import pytest

from bkg_py.cli import main
from bkg_py.result import ExitStatus


@pytest.mark.parametrize(
    ("suffix", "content"),
    [
        (".json", b"null"),
        (".json", b"false"),
        (".json", b"0"),
        (".json", b'""'),
        (".json", b"[]"),
        (".json", b"{}"),
        (".JSON", b' {"version":[{"id":1,"raw_size":-1}]} \n'),
        (".json", b"[18446744073709551616,1e400]"),
        (".xml", b"<xml/>"),
        (".xml", b"<?xml version='1.0'?><xml><package>value</package></xml>"),
    ],
)
def test_validate_accepts_one_complete_document(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    suffix: str,
    content: bytes,
) -> None:
    """Validation checks syntax, not JSON truthiness or metric availability."""

    path = tmp_path / f"output{suffix}"
    path.write_bytes(content)

    assert main(["validate", str(path)]) is ExitStatus.SUCCESS
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""
    assert path.read_bytes() == content


@pytest.mark.parametrize(
    ("suffix", "content"),
    [
        (".json", b" \n\t"),
        (".json", b"{} {}"),
        (".json", b"{} garbage"),
        pytest.param(
            ".json",
            b"[" + b"0," * 40000 + b"0] garbage",
            id="json-late-trailing-garbage",
        ),
        (".json", b"[1,]"),
        (".json", b"NaN"),
        (".json", b"Infinity"),
        (".json", b"-Infinity"),
        (".json", b'{"name":"\xff"}'),
        (".xml", b" \n\t"),
        (".xml", b"<xml><package>"),
        (".xml", b"<xml/><xml/>"),
        pytest.param(
            ".xml",
            b"<xml>" + b"<item/>" * 10000 + b"</xml> garbage",
            id="xml-late-trailing-garbage",
        ),
        (".xml", b"<!DOCTYPE xml><xml/>"),
        (".xml", b'<!DOCTYPE xml [<!ENTITY data "value">]><xml>&data;</xml>'),
        (".xml", b'<!DOCTYPE xml SYSTEM "file:///never-read"><xml/>'),
        (".xml", b'<?xml version="1.0" encoding="unknown"?><xml/>'),
    ],
)
def test_validate_rejects_invalid_documents_without_modifying_them(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    suffix: str,
    content: bytes,
) -> None:
    """Malformed or unsafe input produces failure and a stderr diagnostic."""

    path = tmp_path / f"output{suffix}"
    path.write_bytes(content)

    assert main(["validate", str(path)]) is ExitStatus.NON_FATAL
    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"Invalid {suffix[1:]}: {path}" in captured.err
    assert path.read_bytes() == content


@pytest.mark.parametrize("suffix", [".json", ".xml"])
def test_validate_preserves_empty_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], suffix: str
) -> None:
    """Empty output is a validation failure, not a cleanup request."""

    path = tmp_path / f"output{suffix}"
    path.touch()

    assert main(["validate", str(path)]) is ExitStatus.NON_FATAL
    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"Empty file: {path}" in captured.err
    assert path.read_bytes() == b""


def test_validate_reports_missing_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A missing input cannot be mistaken for successfully validated output."""

    path = tmp_path / "missing.json"

    assert main(["validate", str(path)]) is ExitStatus.NON_FATAL
    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"Cannot read file: {path}" in captured.err
    assert not path.exists()


def test_validate_reports_unreadable_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Read failures return a command error rather than escaping the CLI."""

    path = tmp_path / "output.json"
    path.write_bytes(b"{}")

    with monkeypatch.context() as patch:

        def unreadable(*_args: object, **_kwargs: object) -> None:
            raise PermissionError("denied")

        patch.setattr(Path, "open", unreadable)
        assert main(["validate", str(path)]) is ExitStatus.NON_FATAL

    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"Cannot read file: {path}" in captured.err
    assert path.read_bytes() == b"{}"


def test_validate_rejects_empty_filename(capsys: pytest.CaptureFixture[str]) -> None:
    """An explicitly empty filename is an invalid input."""

    assert main(["validate", ""]) is ExitStatus.NON_FATAL
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Empty file:" in captured.err
