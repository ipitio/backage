"""Tests for application service construction and reuse."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bkg_py.application import ApplicationContext
from bkg_py.cli import main
from bkg_py.database import DatabaseError
from bkg_py.result import ExitStatus


def test_context_constructs_services_lazily_and_reuses_them(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One process shares state, stop control, settings, and its repository."""

    state_path = tmp_path / "state" / "env.env"
    database_path = tmp_path / "index.db"
    monkeypatch.setenv("BKG_ROOT", str(tmp_path))
    monkeypatch.setenv("BKG_ENV", str(state_path))
    monkeypatch.setenv("BKG_INDEX_DB", str(database_path))

    application = ApplicationContext.from_env()

    assert application.state.path == state_path
    assert application.stop.state is application.state
    assert application.database is application.database
    assert application.database.settings.path == database_path
    assert application.aggregate_settings is application.aggregate_settings
    assert application.publication_limits is application.publication_limits
    assert not state_path.exists()

    application.ensure_state_file()
    assert state_path.is_file()


def test_database_configuration_stays_lazy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Commands without database work do not require a database path."""

    monkeypatch.setenv("BKG_ROOT", str(tmp_path))
    monkeypatch.setenv("BKG_ENV", str(tmp_path / "env.env"))
    monkeypatch.delenv("BKG_INDEX_DB", raising=False)

    application = ApplicationContext.from_env()

    with pytest.raises(DatabaseError, match="BKG_INDEX_DB is required"):
        _ = application.database


def test_github_client_uses_shared_runtime_services(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pooled client shares persisted accounting and stop control."""

    state_path = tmp_path / "env.env"
    monkeypatch.setenv("BKG_ROOT", str(tmp_path))
    monkeypatch.setenv("BKG_ENV", str(state_path))
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    application = ApplicationContext.from_env()

    with application.github_client() as client:
        assert client.accounting is not None
        assert client.accounting.state is application.state
        assert getattr(client.runtime.check_stop, "__self__", None) is application.stop
        assert getattr(client.runtime.sleep, "__self__", None) is application.stop

    assert state_path.is_file()


def test_config_cli_remains_independent_of_runtime_services(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The lightweight config command still needs no database or state file."""

    monkeypatch.setenv("BKG_ROOT", str(tmp_path))
    monkeypatch.setenv("BKG_ENV", str(tmp_path / "missing" / "env.env"))
    monkeypatch.delenv("BKG_INDEX_DB", raising=False)

    status = main(["config"])
    output = json.loads(capsys.readouterr().out)

    assert status == ExitStatus.SUCCESS
    assert output["root"] == str(tmp_path)
    assert output["index_db"] is None
    assert not Path(output["env_file"]).exists()
