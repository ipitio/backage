"""Tests for typed runtime configuration."""

from __future__ import annotations

from pathlib import Path

import pytest

from bkg_py.application import ApplicationSettings
from bkg_py.config import ConfigError, RuntimeConfig, SettingsSnapshot


def test_runtime_config_discovers_checkout_from_working_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An installed CLI resolves defaults against the active checkout."""

    checkout = tmp_path / "checkout"
    (checkout / "src" / "bkg_py").mkdir(parents=True)
    working_directory = checkout / "nested"
    working_directory.mkdir()
    monkeypatch.chdir(working_directory)
    for name in (
        "BKG_ROOT",
        "BKG_ENV",
        "BKG_OWNERS",
        "BKG_OPTOUT",
        "BKG_OWNER_ID_CACHE",
    ):
        monkeypatch.delenv(name, raising=False)

    config = RuntimeConfig.from_env()

    assert config.root == str(checkout)
    assert config.env_file == str(checkout / "src" / "env.env")
    assert config.owners_file == str(checkout / "owners.txt")
    assert config.optout_file == str(checkout / "optout.txt")


def test_runtime_config_reads_opt_in_docker_size_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Docker fallback remains disabled unless explicitly configured."""

    monkeypatch.setenv("BKG_DOCKER_SIZE_FALLBACK", "true")
    monkeypatch.setenv("BKG_DOCKER_PLATFORM", "linux/arm64")
    monkeypatch.setenv("BKG_DOCKER_PULL_TIMEOUT", "45")
    monkeypatch.setenv("BKG_DOCKER_COMMAND_TIMEOUT", "6")

    config = RuntimeConfig.from_env()

    assert config.docker_size_fallback
    assert config.docker_platform == "linux/arm64"
    assert config.docker_pull_timeout == 45
    assert config.docker_command_timeout == 6


def test_settings_snapshot_copies_its_source(tmp_path: Path) -> None:
    """A caller cannot change settings by mutating its original mapping."""

    values = {
        "BKG_ROOT": str(tmp_path),
        "BKG_MAX_LEN": "17",
    }
    snapshot = SettingsSnapshot(values)

    values["BKG_MAX_LEN"] = "99"

    assert RuntimeConfig.from_mapping(snapshot).max_len == 17


@pytest.mark.parametrize("value", ["0", "-1"])
def test_runtime_config_preserves_unlimited_duration_values(
    tmp_path: Path,
    value: str,
) -> None:
    """Nonpositive durations retain the documented unlimited behavior."""

    config = RuntimeConfig.from_mapping(
        {
            "BKG_ROOT": str(tmp_path),
            "BKG_MAX_LEN": value,
        }
    )

    assert config.max_len == int(value)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"BKG_MODE": "invalid"}, "BKG_MODE must be an integer"),
        ({"BKG_MODE": "6"}, "BKG_MODE must be between 0 and 5"),
        (
            {"BKG_DOCKER_SIZE_FALLBACK": ""},
            "BKG_DOCKER_SIZE_FALLBACK must be one of",
        ),
        (
            {"BKG_HTTP_MAX_ATTEMPTS": "0"},
            "BKG_HTTP_MAX_ATTEMPTS must be at least 1",
        ),
        (
            {
                "BKG_OWNER_RETRY_INITIAL_SECONDS": "20",
                "BKG_OWNER_RETRY_MAX_SECONDS": "10",
            },
            "BKG_OWNER_RETRY_MAX_SECONDS must be at least",
        ),
        (
            {"BKG_OWNER_ARRAY_DB_ESTIMATE_HEADROOM_PERCENT": "101"},
            "BKG_OWNER_ARRAY_DB_ESTIMATE_HEADROOM_PERCENT must be between 1 and 100",
        ),
        (
            {
                "BKG_JSON_XML_MAX_BYTES": "100",
                "BKG_JSON_XML_HARD_MAX_BYTES": "99",
            },
            "BKG_JSON_XML_HARD_MAX_BYTES must be at least",
        ),
        (
            {"BKG_HTTP_READ_TIMEOUT": "nan"},
            "BKG_HTTP_READ_TIMEOUT must be a finite number",
        ),
    ],
)
def test_application_settings_reject_invalid_explicit_values(
    tmp_path: Path,
    overrides: dict[str, str],
    message: str,
) -> None:
    """Explicit malformed settings fail with their owned variable name."""

    with pytest.raises(ConfigError, match=message):
        ApplicationSettings.from_mapping(
            {
                "BKG_ROOT": str(tmp_path),
                **overrides,
            }
        )
