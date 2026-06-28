"""Tests for package metadata refresh and recoverable publication."""

from __future__ import annotations

import argparse
import json
from dataclasses import replace
from pathlib import Path

import pytest

import bkg_py.package_commands
import bkg_py.package_updates
from bkg_py.application import ApplicationContext
from bkg_py.cli import main
from bkg_py.concurrency import BoundedWorkerRunner, ConcurrencySettings
from bkg_py.database import DatabaseRepository, DatabaseSettings
from bkg_py.database_models import (
    PackageRecord,
    PackageRef,
    VersionMetrics,
    VersionRecord,
    VersionStage,
)
from bkg_py.github import GitHubNotFoundError
from bkg_py.package_updates import (
    PackageOptOuts,
    PackageRefreshError,
    PackageRefreshExecution,
    PackageRefreshPolicy,
    PackageRefreshRequest,
    PackageRefreshResult,
    PackageRefreshService,
)
from bkg_py.publication import PublicationLimits
from bkg_py.result import ExitStatus
from bkg_py.runtime import GracefulStop
from bkg_py.version_selection import VersionSelectionSettings
from bkg_py.version_updates import VersionRefreshExecution

from .github_client_fake import FakeGitHubClient as _FakeClient

_TODAY = "2026-06-26"


def _package() -> PackageRef:
    return PackageRef(
        owner_id="42",
        owner_type="orgs",
        package_type="npm",
        owner="Example",
        repo="Packages",
        package="Demo",
    )


def _version(version_id: str = "7") -> VersionRecord:
    return VersionRecord(
        version_id=version_id,
        name=f"release-{version_id}",
        metrics=VersionMetrics(
            size=123,
            downloads=1_500,
            downloads_month=234,
            downloads_week=56,
            downloads_day=7,
        ),
        date=_TODAY,
        tags="latest",
    )


def _package_record(package: PackageRef) -> PackageRecord:
    return PackageRecord(
        package_ref=package,
        downloads=1_500,
        downloads_month=234,
        downloads_week=56,
        downloads_day=7,
        size=123,
        date=_TODAY,
    )


def _execution(optout_file: Path) -> PackageRefreshExecution:
    return PackageRefreshExecution(
        version=VersionRefreshExecution(
            BoundedWorkerRunner(ConcurrencySettings(max_workers=1)),
            lambda _reference: "",
            today=lambda: _TODAY,
        ),
        selection=VersionSelectionSettings(
            max_version_pages=1,
            max_tag_pages=0,
            append_tagged_limit=0,
        ),
        publication_limits=PublicationLimits(),
        optout_file=optout_file,
        check_stop=lambda: None,
    )


def _request(package: PackageRef, destination: Path) -> PackageRefreshRequest:
    return PackageRefreshRequest(
        package_ref=package,
        legacy_table="legacy_versions",
        since=_TODAY,
        destination=destination,
        policy=PackageRefreshPolicy(
            write_legacy=False,
            use_rest_api=True,
            fast_out=False,
            mode=0,
        ),
    )


def test_optouts_support_literal_and_component_regex_entries() -> None:
    """Owner, repository, package, and component-regex exclusions are retained."""

    package = _package()

    assert PackageOptOuts(("Example",)).matches(package)
    assert PackageOptOuts(("Example/Packages",)).matches(package)
    assert PackageOptOuts(("Example/Packages/Demo",)).matches(package)
    assert PackageOptOuts((r"/^Exa//^Pack//^Dem",)).matches(package)
    assert not PackageOptOuts(("Example/Other",)).matches(package)


def test_package_refresh_cli_dispatches_shell_arguments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The public package command reaches the application operation."""

    captured: list[tuple[str, str]] = []

    def execute(
        args: argparse.Namespace,
        _application: ApplicationContext,
        index_dir: Path,
    ) -> PackageRefreshResult:
        captured.append((args.package, str(index_dir)))
        return PackageRefreshResult("refreshed", package_written=True)

    monkeypatch.setattr(
        bkg_py.package_commands,
        "_execute_package_refresh",
        execute,
    )
    monkeypatch.setenv("BKG_ROOT", str(tmp_path))
    monkeypatch.setenv("BKG_INDEX_DIR", str(tmp_path / "index"))

    status = main(
        [
            "package",
            "refresh",
            "42",
            "orgs",
            "npm",
            "Example",
            "Packages",
            "Demo",
            "legacy_versions",
            _TODAY,
            "false",
            "true",
            "false",
        ]
    )

    assert status == ExitStatus.SUCCESS
    assert captured == [("Demo", str(tmp_path / "index"))]
    assert json.loads(capsys.readouterr().out)["outcome"] == "refreshed"


def test_missing_package_detail_stays_pending_without_response_body_diagnostic(
    tmp_path: Path,
) -> None:
    """An expected missing stale package does not dump its GitHub HTML body."""

    package = _package()
    destination = tmp_path / "index" / "Example" / "Packages" / "Demo.json"
    optout_file = tmp_path / "optout.txt"
    optout_file.write_text("", encoding="utf-8")
    package_url = "https://github.com/orgs/Example/packages/npm/package/Demo"
    diagnostics: list[str] = []
    execution = _execution(optout_file)
    execution = replace(
        execution,
        version=replace(execution.version, diagnostic=diagnostics.append),
    )
    client = _FakeClient(
        text_values={package_url: GitHubNotFoundError("large HTML response")}
    )

    result = PackageRefreshService(
        DatabaseRepository(DatabaseSettings(tmp_path / "index.db")),
        client,
        execution,
    ).refresh(_request(package, destination))

    assert result.outcome == "metadata_unavailable"
    assert not diagnostics


def test_refresh_commits_versions_package_and_publication(
    tmp_path: Path,
) -> None:
    """One Python operation owns network ingestion through JSON/XML publication."""

    package = _package()
    repository = DatabaseRepository(DatabaseSettings(tmp_path / "index.db"))
    destination = tmp_path / "index" / package.owner / package.repo / "Demo.json"
    optout_file = tmp_path / "optout.txt"
    optout_file.write_text("", encoding="utf-8")
    api_path = "orgs/Example/packages/npm/Demo/versions?per_page=30&page=1"
    package_url = "https://github.com/orgs/Example/packages/npm/package/Demo"
    version_url = "https://github.com/orgs/Example/packages/npm/Demo/7"
    metrics = (
        "<span>Total downloads</span><span>1.5k</span>"
        "<span>Last 30 days</span><span>234</span>"
        "<span>Last week</span><span>56</span>"
        "<span>Today</span><span>7</span>"
    )
    client = _FakeClient(
        rest_values={api_path: [{"id": 7, "name": "release-7", "tags": ["latest"]}]},
        text_values={package_url: metrics, version_url: metrics},
    )

    result = PackageRefreshService(
        repository,
        client,
        _execution(optout_file),
    ).refresh(_request(package, destination))

    assert result.outcome == "refreshed"
    assert result.package_written
    assert result.version_refresh is not None
    assert result.version_refresh.records_written == 1
    assert destination.is_file()
    assert destination.with_suffix(".xml").is_file()
    assert not repository.package_publication_pending(package)
    rendered = json.loads(destination.read_text(encoding="utf-8"))
    assert rendered["raw_downloads"] == 1_500
    assert rendered["raw_size"] == -1
    assert rendered["version"][0]["id"] == 7
    assert client.rest_requests == [api_path]
    assert client.text_requests == [package_url, version_url]
    assert client.text_authentication == [True, True]


def test_refresh_rejects_a_publication_marker_that_did_not_clear(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refresh cannot report success while its files remain pending."""

    package = _package()
    repository = DatabaseRepository(DatabaseSettings(tmp_path / "index.db"))
    destination = tmp_path / "index" / package.owner / package.repo / "Demo.json"
    optout_file = tmp_path / "optout.txt"
    optout_file.write_text("", encoding="utf-8")
    package_url = "https://github.com/orgs/Example/packages/npm/package/Demo"
    api_path = "orgs/Example/packages/npm/Demo/versions?per_page=30&page=1"
    versions_url = "https://github.com/orgs/Example/packages/npm/Demo/versions?page=1"
    metrics = "<span>Total downloads</span><span>1</span>"

    def leave_pending(_package: PackageRef) -> None:
        pass

    monkeypatch.setattr(repository, "clear_package_publication", leave_pending)

    with pytest.raises(PackageRefreshError, match="publication marker still pending"):
        PackageRefreshService(
            repository,
            _FakeClient(
                rest_values={api_path: []},
                text_values={package_url: metrics, versions_url: "<div></div>"},
            ),
            _execution(optout_file),
        ).refresh(_request(package, destination))

    assert repository.package_publication_pending(package)


def test_interrupted_publication_keeps_old_files_and_pending_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Committed data remains queued when publication stops before replacement."""

    package = _package()
    repository = DatabaseRepository(DatabaseSettings(tmp_path / "index.db"))
    repository.write_package(_package_record(package))
    repository.flush_version_stage(
        VersionStage(package, "legacy_versions", False, (_version(),))
    )
    destination = tmp_path / "index" / package.owner / package.repo / "Demo.json"
    destination.parent.mkdir(parents=True)
    destination.write_text('{"old":true}\n', encoding="utf-8")
    xml_path = destination.with_suffix(".xml")
    xml_path.write_text("<xml><old>true</old></xml>\n", encoding="utf-8")

    def stop_publication(*_args: object, **_kwargs: object) -> None:
        raise GracefulStop("test-stop")

    monkeypatch.setattr(
        bkg_py.package_updates,
        "publish_json_file",
        stop_publication,
    )

    with pytest.raises(GracefulStop, match="test-stop"):
        PackageRefreshService(
            repository,
            _FakeClient(),
            _execution(tmp_path / "missing-optout.txt"),
        ).refresh(_request(package, destination))

    assert repository.package_publication_pending(package)
    assert destination.read_text(encoding="utf-8") == '{"old":true}\n'
    assert xml_path.read_text(encoding="utf-8") == "<xml><old>true</old></xml>\n"


def test_pending_publication_retries_without_network_requests(tmp_path: Path) -> None:
    """A later package operation publishes committed rows and clears the marker."""

    package = _package()
    repository = DatabaseRepository(DatabaseSettings(tmp_path / "index.db"))
    repository.write_package_pending_publication(_package_record(package))
    repository.flush_version_stage(
        VersionStage(package, "legacy_versions", False, (_version(),)),
        publication_pending_at=_TODAY,
    )
    destination = tmp_path / "index" / package.owner / package.repo / "Demo.json"
    client = _FakeClient()

    result = PackageRefreshService(
        repository,
        client,
        _execution(tmp_path / "missing-optout.txt"),
    ).refresh(_request(package, destination))

    assert result.outcome == "refreshed"
    assert not result.package_written
    assert not repository.package_publication_pending(package)
    assert not client.rest_requests
    assert not client.text_requests


def test_opted_out_package_removes_database_marker_and_files(tmp_path: Path) -> None:
    """Package opt-out cleanup removes normalized and publication state together."""

    package = _package()
    repository = DatabaseRepository(DatabaseSettings(tmp_path / "index.db"))
    repository.write_package_pending_publication(_package_record(package))
    repository.flush_version_stage(
        VersionStage(package, "legacy_versions", False, (_version(),)),
        publication_pending_at=_TODAY,
    )
    destination = tmp_path / "index" / package.owner / package.repo / "Demo.json"
    destination.parent.mkdir(parents=True)
    destination.write_text("{}\n", encoding="utf-8")
    destination.with_suffix(".xml").write_text("<xml></xml>\n", encoding="utf-8")
    optout_file = tmp_path / "optout.txt"
    optout_file.write_text("Example/Packages/Demo\n", encoding="utf-8")

    result = PackageRefreshService(
        repository,
        _FakeClient(),
        _execution(optout_file),
    ).refresh(_request(package, destination))

    assert result.outcome == "opted_out"
    assert repository.package_snapshot(package, since=_TODAY) is None
    assert not repository.package_publication_pending(package)
    assert not destination.exists()
    assert not destination.with_suffix(".xml").exists()
