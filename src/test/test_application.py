"""Tests for application service construction and reuse."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

import bkg_py.cli
from bkg_py.application import ApplicationContext
from bkg_py.cli import build_parser, entrypoint, main
from bkg_py.database import DatabaseError
from bkg_py.result import ExitStatus
from bkg_py.workspace import update as workspace_update


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
    assert "metric_enrichment" in vars(application)
    assert application.metric_enrichment is application.metric_enrichment
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


def test_run_configuration_rebinds_stop_aware_services(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run overrides cannot leave cached services on the startup controller."""

    monkeypatch.setenv("BKG_ROOT", str(tmp_path))
    monkeypatch.setenv("BKG_ENV", str(tmp_path / "env.env"))
    monkeypatch.setenv("BKG_INDEX_DB", str(tmp_path / "index.db"))
    application = ApplicationContext.from_env()
    old_stop = application.stop
    old_database = application.database
    old_snapshots = application.snapshots
    old_worker = application.worker_runner
    old_metrics = application.metric_enrichment
    old_version_listings = application.version_listing_recovery
    old_process_runner = application.process_runner

    application.configure_run(
        replace(application.config, max_len=17),
        started_at_epoch=application.stop.timing.wall_clock(),
    )

    assert application.stop is not old_stop
    assert application.stop.max_duration == 17
    assert application.database is not old_database
    assert application.snapshots is not old_snapshots
    assert application.worker_runner is not old_worker
    assert application.metric_enrichment is not old_metrics
    assert application.version_listing_recovery is not old_version_listings
    assert application.process_runner is not old_process_runner
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


@pytest.mark.parametrize(
    ("run_date", "expected"),
    [
        ("2026-07-01", "v2026.7.0"),
        ("2026-07-14", "v2026.7.0"),
        ("2026-07-15", "v2026.7.1"),
        ("2026-07-28", "v2026.7.1"),
        ("2026-07-29", "v2026.7.2"),
    ],
)
def test_release_tag_cli_owns_the_fortnightly_workflow_policy(
    run_date: str,
    expected: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Runtime events and release publication share one period calculation."""

    assert main(["release-tag", "-D", run_date]) is ExitStatus.SUCCESS
    assert capsys.readouterr().out.strip() == expected


@pytest.mark.parametrize(
    ("short_arguments", "long_arguments"),
    [
        (
            [
                "run",
                "-d",
                "17",
                "-m",
                "2",
                "-C",
                "work",
                "-n",
                "7",
                "-D",
                "2026-07-28",
            ],
            [
                "run",
                "--duration",
                "17",
                "--mode",
                "2",
                "--working-directory",
                "work",
                "--owner-request-limit",
                "7",
                "--run-date",
                "2026-07-28",
            ],
        ),
        (
            [
                "workflow-update",
                "-r",
                "root",
                "-d",
                "17",
                "-m",
                "2",
                "-C",
                "invocation",
                "-n",
                "7",
                "-p",
                "payload",
                "-D",
                "2026-07-28",
            ],
            [
                "workflow-update",
                "--root",
                "root",
                "--duration",
                "17",
                "--mode",
                "2",
                "--invocation-directory",
                "invocation",
                "--owner-request-limit",
                "7",
                "--payload-directory",
                "payload",
                "--run-date",
                "2026-07-28",
            ],
        ),
    ],
)
def test_operational_short_options_match_long_forms(
    short_arguments: list[str],
    long_arguments: list[str],
) -> None:
    """Concise operational options preserve their descriptive forms."""

    parser = build_parser()

    assert parser.parse_args(short_arguments) == parser.parse_args(long_arguments)


@pytest.mark.parametrize(
    ("arguments", "expected_root"),
    [
        (["workflow-update"], Path("bkg")),
        (["workflow-update", "positional"], Path("positional")),
        (["workflow-update", "-r", "short"], Path("short")),
        (["workflow-update", "--root", "named"], Path("named")),
    ],
)
def test_workflow_update_root_forms(
    arguments: list[str],
    expected_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The named root, compatibility root, and workflow default agree."""

    observed: list[Path] = []

    def capture_request(
        request: workspace_update.UpdateWorkflowRequest,
        _execution: workspace_update.UpdateWorkflowExecution,
    ) -> ExitStatus:
        observed.append(request.root)
        return ExitStatus.SUCCESS

    monkeypatch.setattr(workspace_update, "run_update_workflow", capture_request)

    assert main(arguments) is ExitStatus.SUCCESS
    assert observed == [expected_root]


def test_workflow_update_rejects_positional_and_named_roots(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """One invocation cannot select two repository roots."""

    with pytest.raises(SystemExit) as raised:
        main(["workflow-update", "positional", "--root", "named"])

    assert raised.value.code == 2
    assert "ROOT and --root cannot be used together" in capsys.readouterr().err


def test_run_date_requires_extended_iso_form(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The workflow boundary accepts one unambiguous persisted date form."""

    with pytest.raises(SystemExit) as raised:
        build_parser().parse_args(["run", "--run-date", "20260728"])

    assert raised.value.code == 2
    assert "date must use YYYY-MM-DD" in capsys.readouterr().err


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
