"""Tests for typed runtime configuration."""

from __future__ import annotations

from pathlib import Path

import pytest

from bkg_py.config import RuntimeConfig


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
