"""Tests for detailed package-version refresh orchestration."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import pytest

from bkg_py.application import ApplicationContext
from bkg_py.cli import main
from bkg_py.concurrency import BoundedWorkerRunner, ConcurrencySettings
from bkg_py.database import DatabaseRepository, DatabaseSettings
from bkg_py.database_models import (
    PackageRef,
    VersionMetrics,
    VersionRecord,
    VersionStage,
)
from bkg_py.result import ExitStatus
from bkg_py.runtime import GracefulStop
from bkg_py.version_selection import VersionCandidate, VersionSelectionSettings
from bkg_py.version_updates import (
    VersionDetailExecution,
    VersionDetailInspector,
    VersionRefreshExecution,
    VersionRefreshRequest,
    VersionRefreshService,
)
from bkg_py.versions import VersionListingContext

from .github_client_fake import FakeGitHubClient as _FakeClient

_TODAY = "2026-06-20"


_CONTEXT = VersionListingContext(
    owner_type="orgs",
    owner="Example",
    repo="Packages",
    package_type="container",
    package="Nested%2FImage",
)


def _detail_url(version_id: str, *, package_type: str = "container") -> str:
    return (
        f"https://github.com/orgs/Example/packages/{package_type}/"
        f"Nested%2FImage/{version_id}"
    )


def _package_detail_url(*, package_type: str = "container") -> str:
    return (
        f"https://github.com/orgs/Example/packages/{package_type}/"
        "package/Nested%2FImage"
    )


def _metrics_html(*, manifest: str = "") -> str:
    manifest_html = (
        f"<h4>Manifest</h4><code><pre>{manifest}</pre></code>" if manifest else ""
    )
    return (
        "<span>Total downloads</span><span>1.5k</span>"
        "<span>Last 30 days</span><span>234</span>"
        "<span>Last week</span><span>56</span>"
        "<span>Today</span><span>7</span>"
        f"{manifest_html}"
    )


def _package_ref(*, package_type: str = "npm") -> PackageRef:
    return PackageRef(
        owner_id="42",
        owner_type="orgs",
        package_type=package_type,
        owner="Example",
        repo="Packages",
        package="Nested%2FImage",
    )


def _record(version_id: str) -> VersionRecord:
    return VersionRecord(
        version_id=version_id,
        name=f"version-{version_id}",
        metrics=VersionMetrics(1, 2, 3, 4, 5),
        date=_TODAY,
        tags="",
    )


def _api_version(version_id: int) -> dict[str, object]:
    return {
        "id": version_id,
        "name": f"version-{version_id}",
        "tags": [],
    }


def test_detail_inspector_uses_embedded_manifest_and_oci_label() -> None:
    """Embedded metrics, layer sizes, and an OCI label form one version row."""

    manifest = (
        '{"layers":[{"size":10},{"size":25}],"config":'
        '{"org.opencontainers.image.version":"v1"}}'
    )
    client = _FakeClient(
        text_values={_detail_url("7"): _metrics_html(manifest=manifest)}
    )
    inspected_references: list[str] = []
    inspector = VersionDetailInspector(
        client,
        _CONTEXT,
        VersionDetailExecution(
            lambda reference: inspected_references.append(reference) or ""
        ),
    )

    record = inspector.inspect(VersionCandidate("7", "sha256:abc"), today=_TODAY)

    assert record.metrics == VersionMetrics(35, 1500, 234, 56, 7)
    assert record.tags == "v1"
    assert not inspected_references


def test_detail_inspector_leaves_unknown_size_after_manifest_fallbacks() -> None:
    """Unknown container sizes do not depend on an external badge service."""

    client = _FakeClient(
        text_values={
            _detail_url("8"): "",
            _detail_url("9"): "",
        }
    )
    references: list[str] = []
    inspector = VersionDetailInspector(
        client,
        _CONTEXT,
        VersionDetailExecution(
            lambda reference: references.append(reference) or "",
            authenticated=True,
        ),
    )

    first = inspector.inspect(VersionCandidate("8", "latest"), today=_TODAY)
    second = inspector.inspect(
        VersionCandidate("9", "sha256:def"),
        today=_TODAY,
    )

    assert first.metrics.size == -1
    assert second.metrics.size == -1
    assert references == [
        "ghcr.io/example/nested/image:latest",
        "ghcr.io/example/nested/image@sha256:def",
    ]
    assert client.text_requests == [_detail_url("8"), _detail_url("9")]
    assert client.text_authentication == [True, True]


def test_detail_inspector_uses_registry_manifest_after_page_fallback() -> None:
    """A registry manifest supplies size after the page lacks a manifest."""

    client = _FakeClient(text_values={_detail_url("10"): ""})
    inspector = VersionDetailInspector(
        client,
        _CONTEXT,
        VersionDetailExecution(
            lambda _reference: '{"layers":[{"size":10},{"size":20}]}'
        ),
    )

    record = inspector.inspect(VersionCandidate("10", "stable"), today=_TODAY)

    assert record.metrics.size == 30
    assert client.text_requests == [_detail_url("10")]


def test_detail_inspector_uses_package_page_for_fallback_candidate() -> None:
    """The package-level fallback does not request a fake version ID."""

    package_context = VersionListingContext(
        owner_type="orgs",
        owner="Example",
        repo="Packages",
        package_type="npm",
        package="Nested%2FImage",
    )
    client = _FakeClient(
        text_values={_package_detail_url(package_type="npm"): _metrics_html()}
    )
    inspector = VersionDetailInspector(
        client,
        package_context,
        VersionDetailExecution(lambda _reference: ""),
    )

    record = inspector.inspect(VersionCandidate("-1", "latest"), today=_TODAY)

    assert record.version_id == "-1"
    assert record.name == "latest"
    assert record.metrics.downloads == 1500
    assert client.text_requests == [_package_detail_url(package_type="npm")]


def test_refresh_skips_existing_versions_and_flushes_one_batch(tmp_path: Path) -> None:
    """Selection avoids current rows and persists inspected rows transactionally."""

    package = _package_ref()
    repository = DatabaseRepository(DatabaseSettings(tmp_path / "index.db"))
    repository.flush_version_stage(
        VersionStage(package, "legacy_versions", False, (_record("1"),))
    )
    api_path = "orgs/Example/packages/npm/Nested%2FImage/versions?per_page=30&page=1"
    client = _FakeClient(
        rest_values={
            api_path: [
                _api_version(2),
                _api_version(1),
            ]
        },
        text_values={_detail_url("2", package_type="npm"): _metrics_html()},
    )
    service = VersionRefreshService(
        repository,
        client,
        VersionRefreshExecution(
            BoundedWorkerRunner(ConcurrencySettings(max_workers=2)),
            lambda _reference: "",
            lambda _message: None,
            lambda: _TODAY,
        ),
    )

    result = service.refresh(
        VersionRefreshRequest(package, "legacy_versions", False, True, _TODAY),
        VersionSelectionSettings(max_tag_pages=0, append_tagged_limit=0),
    )

    rows = repository.version_rows(package, since=_TODAY).rows
    assert result.records_written == 1
    assert result.selection.selected_ids == ("2", "1")
    assert [row.version_id for row in rows] == ["1", "2"]
    assert client.text_requests == [_detail_url("2", package_type="npm")]


def test_refresh_flushes_completed_rows_before_graceful_stop(tmp_path: Path) -> None:
    """A completed detail row survives when the following worker requests stop."""

    package = _package_ref()
    stopped = False

    def check_stop() -> None:
        if stopped:
            raise GracefulStop("test stop")

    def request_stop() -> str:
        nonlocal stopped
        stopped = True
        raise GracefulStop("test stop")

    database_path = tmp_path / "index.db"
    repository = DatabaseRepository(
        DatabaseSettings(database_path),
        check_stop=check_stop,
    )
    api_path = "orgs/Example/packages/npm/Nested%2FImage/versions?per_page=30&page=1"
    client = _FakeClient(
        rest_values={
            api_path: [
                _api_version(1),
                _api_version(2),
            ]
        },
        text_values={
            _detail_url("1", package_type="npm"): _metrics_html(),
            _detail_url("2", package_type="npm"): request_stop,
        },
    )
    service = VersionRefreshService(
        repository,
        client,
        VersionRefreshExecution(
            BoundedWorkerRunner(
                ConcurrencySettings(max_workers=1),
                check_stop=check_stop,
            ),
            lambda _reference: "",
            lambda _message: None,
            lambda: _TODAY,
        ),
    )

    with pytest.raises(GracefulStop, match="test stop"):
        service.refresh(
            VersionRefreshRequest(package, "legacy_versions", False, True, _TODAY),
            VersionSelectionSettings(max_tag_pages=0, append_tagged_limit=0),
        )

    with sqlite3.connect(database_path) as connection:
        rows = connection.execute("select id from versions order by id").fetchall()
    assert rows == [("1",)]


def test_refresh_package_cli_wires_runtime_services(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The shell-facing refresh command runs one complete package update."""

    package = _package_ref()
    api_path = "orgs/Example/packages/npm/Nested%2FImage/versions?per_page=30&page=1"
    client = _FakeClient(
        rest_values={api_path: [_api_version(3)]},
        text_values={_detail_url("3", package_type="npm"): _metrics_html()},
    )

    @contextmanager
    def fake_github_client(
        _application: ApplicationContext,
    ) -> Generator[_FakeClient, None, None]:
        yield client

    database_path = tmp_path / "index.db"
    monkeypatch.setenv("BKG_ROOT", str(tmp_path))
    monkeypatch.setenv("BKG_ENV", str(tmp_path / "env.env"))
    monkeypatch.setenv("BKG_INDEX_DB", str(database_path))
    monkeypatch.setenv("BKG_MAX_VERSION_PAGES", "1")
    monkeypatch.setenv("BKG_TAG_CACHE_PAGES", "0")
    monkeypatch.setenv("BKG_APPEND_TAGGED_VERSIONS_LIMIT", "0")
    monkeypatch.setattr(ApplicationContext, "github_client", fake_github_client)

    status = main(
        [
            "version",
            "refresh-package",
            package.owner_id,
            package.owner_type,
            package.package_type,
            package.owner,
            package.repo,
            package.package,
            "legacy_versions",
            _TODAY,
            "false",
            "true",
        ]
    )
    captured = capsys.readouterr()

    assert status == ExitStatus.SUCCESS
    assert json.loads(captured.out) == {
        "selected_ids": ["3"],
        "candidate_count": 1,
        "records_written": 1,
        "version_pages_read": 1,
        "tag_pages_read": 0,
        "used_fallback": False,
    }
    assert captured.err == ""
    with sqlite3.connect(database_path) as connection:
        rows = connection.execute(
            "select owner_id, package_type, id, downloads from versions order by id"
        ).fetchall()
    assert rows == [(package.owner_id, package.package_type, "3", 1500)]
