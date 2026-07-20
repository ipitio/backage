"""Tests for application service construction and reuse."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import bkg_py.cli
from bkg_py.application import ApplicationContext
from bkg_py.cli import entrypoint, main
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


def test_database_settings_use_captured_runtime_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Database paths and table names come from one captured config object."""

    original_database_path = tmp_path / "index.db"
    monkeypatch.setenv("BKG_ROOT", str(tmp_path))
    monkeypatch.setenv("BKG_ENV", str(tmp_path / "env.env"))
    monkeypatch.setenv("BKG_INDEX_DB", str(original_database_path))
    monkeypatch.setenv("BKG_INDEX_TBL_OWN", "captured_owners")
    monkeypatch.setenv("BKG_INDEX_TBL_PKG", "captured_packages")
    monkeypatch.setenv("BKG_INDEX_TBL_VER", "captured_versions")
    application = ApplicationContext.from_env()

    monkeypatch.setenv("BKG_INDEX_DB", str(tmp_path / "changed.db"))
    monkeypatch.setenv("BKG_INDEX_TBL_OWN", "changed_owners")
    monkeypatch.setenv("BKG_INDEX_TBL_PKG", "changed_packages")
    monkeypatch.setenv("BKG_INDEX_TBL_VER", "changed_versions")

    settings = application.database.settings

    assert settings.path == original_database_path
    assert settings.owners_table == "captured_owners"
    assert settings.packages_table == "captured_packages"
    assert settings.versions_table == "captured_versions"


def test_version_selection_settings_use_captured_runtime_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Version page limits are captured once for one application process."""

    monkeypatch.setenv("BKG_ROOT", str(tmp_path))
    monkeypatch.setenv("BKG_MAX_VERSION_PAGES", "4")
    monkeypatch.setenv("BKG_TAG_CACHE_PAGES", "2")
    monkeypatch.setenv("BKG_APPEND_TAGGED_VERSIONS_LIMIT", "17")
    application = ApplicationContext.from_env()

    monkeypatch.setenv("BKG_MAX_VERSION_PAGES", "99")
    monkeypatch.setenv("BKG_TAG_CACHE_PAGES", "99")
    monkeypatch.setenv("BKG_APPEND_TAGGED_VERSIONS_LIMIT", "99")

    settings = application.version_selection_settings

    assert settings.max_version_pages == 4
    assert settings.max_tag_pages == 2
    assert settings.append_tagged_limit == 17
    assert application.version_selection_settings is settings


def test_worker_settings_use_captured_runtime_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Worker limits are captured once and reused by runtime services."""

    monkeypatch.setenv("BKG_ROOT", str(tmp_path))
    monkeypatch.setenv("BKG_PARALLEL_ASYNC_MAX_JOBS", "7")
    monkeypatch.setenv("BKG_OWNER_UPDATE_STOP_GRACE", "14.5")
    application = ApplicationContext.from_env()

    monkeypatch.setenv("BKG_PARALLEL_ASYNC_MAX_JOBS", "99")
    monkeypatch.setenv("BKG_OWNER_UPDATE_STOP_GRACE", "99")

    settings = application.concurrency_settings

    assert settings.max_workers == 7
    assert settings.stop_grace_seconds == 14.5
    assert application.concurrency_settings is settings
    assert application.worker_runner.settings is settings
    assert (
        getattr(application.worker_runner.check_stop, "__self__", None)
        is application.stop
    )
    assert application.process_runner.stop is application.stop


def test_github_client_uses_shared_runtime_services(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pooled client shares persisted accounting and stop control."""

    state_path = tmp_path / "env.env"
    monkeypatch.setenv("BKG_ROOT", str(tmp_path))
    monkeypatch.setenv("BKG_ENV", str(state_path))
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setenv("BKG_GITHUB_REST_RESERVE", "17")
    application = ApplicationContext.from_env()

    with application.github_client() as client:
        assert client.accounting is not None
        assert client.accounting.state is application.state
        assert client.accounting is application.github_rate_accounting
        assert client.accounting.rest_reserve == 17
        assert getattr(client.runtime.check_stop, "__self__", None) is application.stop
        assert (
            getattr(client.runtime.request_stop, "__self__", None) is application.stop
        )
        assert getattr(client.runtime.sleep, "__self__", None) is application.stop

    with application.github_client() as second_client:
        assert second_client.accounting is application.github_rate_accounting

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


def test_entrypoint_collapses_internal_failure_status(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The process boundary exposes only public exit statuses."""

    monkeypatch.setattr(bkg_py.cli, "main", lambda: ExitStatus.FAILURE)

    with pytest.raises(SystemExit) as raised:
        entrypoint()

    assert raised.value.code == ExitStatus.NON_FATAL
    assert (
        capsys.readouterr().err.strip()
        == "Unexpected bkg status 2 (FAILURE); returning 1"
    )
