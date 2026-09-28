"""Tests for package metadata refresh and recoverable publication."""

import json
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

import bkg_py.packages.updates
from bkg_py.concurrency import BoundedWorkerRunner, ConcurrencySettings
from bkg_py.database.composition import DatabaseRepositories
from bkg_py.database.models import (
    PackageRecord,
    PackageRef,
    VersionMetrics,
    VersionRecord,
    VersionStage,
)
from bkg_py.database.settings import DatabaseSettings
from bkg_py.github import GitHubNotFoundError, GitHubTransportError
from bkg_py.packages.enrichment import METRIC_TEXT_REQUEST_POLICY
from bkg_py.packages.registry.artifacts import (
    ArtifactSizeRequest,
    ArtifactSizeResolver,
    ArtifactSizeResult,
    ArtifactSizeSemantics,
)
from bkg_py.packages.updates import (
    PackageOptOuts,
    PackageRefreshError,
    PackageRefreshExecution,
    PackageRefreshPolicy,
    PackageRefreshRequest,
    PackageRefreshService,
)
from bkg_py.packages.versions.metadata import DownloadMetrics
from bkg_py.packages.versions.selection import VersionSelectionSettings
from bkg_py.packages.versions.updates import VersionRefreshExecution
from bkg_py.publication import PublicationLimits
from bkg_py.runtime import GracefulStop

from ..github.fake import FakeGitHubClient as _FakeClient

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


def _metrics_html(metrics: DownloadMetrics) -> str:
    return "".join(
        f'<span>{label}</span><h3 title="{value}">{value}</h3>'
        for label, value in (
            ("Total downloads", metrics.total),
            ("Last 30 days", metrics.month),
            ("Last week", metrics.week),
            ("Today", metrics.day),
        )
        if value >= 0
    )


@dataclass(frozen=True)
class _FixedSizeAdapter:
    size: int

    def resolve(self, request: ArtifactSizeRequest) -> ArtifactSizeResult:
        """Return one fixed compressed-download size."""

        return ArtifactSizeResult(
            self.size,
            ArtifactSizeSemantics.COMPRESSED_DOWNLOAD,
            f"test-{request.context.package_type}-metadata",
        )


def _execution(
    optout_file: Path,
    *,
    size_resolver: ArtifactSizeResolver | None = None,
) -> PackageRefreshExecution:
    return PackageRefreshExecution(
        version=VersionRefreshExecution(
            BoundedWorkerRunner(ConcurrencySettings(max_workers=1)),
            ArtifactSizeResolver() if size_resolver is None else size_resolver,
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


def test_external_enrichment_policy_respects_private_capable_modes() -> None:
    """Private-capable modes authenticate GitHub and avoid hosted sizing."""

    assert not PackageRefreshPolicy(False, True, 0).authenticate_html
    assert PackageRefreshPolicy(False, True, 3).authenticate_html
    assert not PackageRefreshPolicy(False, False, 3).authenticate_html
    assert PackageRefreshPolicy(False, True, 0).allow_hosted_size_fallback
    assert not PackageRefreshPolicy(False, True, 3).allow_hosted_size_fallback
    assert not PackageRefreshPolicy(False, False, 3).allow_hosted_size_fallback


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
        DatabaseRepositories(DatabaseSettings(tmp_path / "index.db")).packages,
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
    repository = DatabaseRepositories(DatabaseSettings(tmp_path / "index.db")).packages
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
        _execution(
            optout_file,
            size_resolver=ArtifactSizeResolver({"npm": _FixedSizeAdapter(0)}),
        ),
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
    assert rendered["raw_size"] == 0
    assert rendered["version"][0]["id"] == 7
    assert client.rest_requests == [api_path]
    assert client.text_requests == [package_url, version_url]
    assert client.text_authentication == [False, False]
    assert client.text_policies == [
        METRIC_TEXT_REQUEST_POLICY,
        METRIC_TEXT_REQUEST_POLICY,
    ]


def test_transient_package_metrics_remain_unknown_while_versions_continue(
    tmp_path: Path,
) -> None:
    """Optional package metrics do not block version and publication work."""

    package = _package()
    repository = DatabaseRepositories(DatabaseSettings(tmp_path / "index.db")).packages
    repository.write_package(replace(_package_record(package), date="2026-06-25"))
    destination = tmp_path / "index" / package.owner / package.repo / "Demo.json"
    optout_file = tmp_path / "optout.txt"
    optout_file.write_text("", encoding="utf-8")
    api_path = "orgs/Example/packages/npm/Demo/versions?per_page=30&page=1"
    package_url = "https://github.com/orgs/Example/packages/npm/package/Demo"
    version_url = "https://github.com/orgs/Example/packages/npm/Demo/8"
    client = _FakeClient(
        rest_values={api_path: [{"id": 8, "name": "release-8", "tags": ["edge"]}]},
        text_values={
            package_url: GitHubTransportError("temporary package metrics failure"),
            version_url: "<span>Total downloads</span><span>4</span>",
        },
    )

    result = PackageRefreshService(
        repository,
        client,
        _execution(optout_file),
    ).refresh(_request(package, destination))

    rendered = json.loads(destination.read_text(encoding="utf-8"))
    assert result.outcome == "refreshed"
    assert [
        rendered[field]
        for field in (
            "raw_downloads",
            "raw_downloads_month",
            "raw_downloads_week",
            "raw_downloads_day",
        )
    ] == [-1, -1, -1, -1]
    assert rendered["version"][0]["raw_downloads"] == 4


@pytest.mark.parametrize(
    "metrics",
    [
        DownloadMetrics(10_000, 2_000, 300, 40),
        DownloadMetrics(10, 5, 2, 1),
        DownloadMetrics(0, 0, 0, 0),
        DownloadMetrics(25, -1, -1, -1),
        DownloadMetrics(-1, 20, -1, 0),
        DownloadMetrics(-1, -1, -1, -1),
    ],
    ids=["larger", "decreased", "zero", "total-only", "rolling-only", "unavailable"],
)
def test_package_counters_use_the_current_page_not_sampled_versions(
    tmp_path: Path, metrics: DownloadMetrics
) -> None:
    """Current package counters never inherit history or sampled version sums."""

    package = _package()
    repository = DatabaseRepositories(DatabaseSettings(tmp_path / "index.db")).packages
    repository.write_package(replace(_package_record(package), date="2026-06-25"))
    destination = tmp_path / "index" / package.owner / package.repo / "Demo.json"
    api_path = "orgs/Example/packages/npm/Demo/versions?per_page=30&page=1"
    package_url = "https://github.com/orgs/Example/packages/npm/package/Demo"
    version_url = "https://github.com/orgs/Example/packages/npm/Demo/8"
    version_metrics = DownloadMetrics(1_500, 234, 56, 7)
    client = _FakeClient(
        rest_values={api_path: [{"id": 8, "name": "release-8", "tags": ["edge"]}]},
        text_values={
            package_url: _metrics_html(metrics),
            version_url: _metrics_html(version_metrics),
        },
    )

    result = PackageRefreshService(
        repository, client, _execution(tmp_path / "optout.txt")
    ).refresh(_request(package, destination))

    assert result.outcome == "refreshed"
    snapshot = repository.package_snapshot(package, since=_TODAY)
    assert snapshot is not None
    assert snapshot.package.record == PackageRecord(
        package, metrics.total, metrics.month, metrics.week, metrics.day, -1, _TODAY
    )
    rendered = json.loads(destination.read_text(encoding="utf-8"))
    fields = (
        "raw_downloads",
        "raw_downloads_month",
        "raw_downloads_week",
        "raw_downloads_day",
    )
    assert tuple(rendered[field] for field in fields) == (
        metrics.total,
        metrics.month,
        metrics.week,
        metrics.day,
    )
    assert tuple(rendered["version"][0][field] for field in fields) == (
        version_metrics.total,
        version_metrics.month,
        version_metrics.week,
        version_metrics.day,
    )
    assert client.text_requests == [package_url, version_url]


def test_refresh_rejects_a_publication_marker_that_did_not_clear(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refresh cannot report success while its files remain pending."""

    package = _package()
    repository = DatabaseRepositories(DatabaseSettings(tmp_path / "index.db")).packages
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
    repository = DatabaseRepositories(DatabaseSettings(tmp_path / "index.db")).packages
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
        bkg_py.packages.updates,
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
    repository = DatabaseRepositories(DatabaseSettings(tmp_path / "index.db")).packages
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


@pytest.mark.parametrize("mode", [0, 1])
def test_republication_preserves_completed_metrics_and_observation_date(
    tmp_path: Path, mode: int
) -> None:
    """Publishing an existing observation does not synthesize a new one."""

    package = _package()
    repository = DatabaseRepositories(DatabaseSettings(tmp_path / "index.db")).packages
    record = replace(_package_record(package), date="2026-06-25")
    repository.write_package_pending_publication(record)
    repository.flush_version_stage(
        VersionStage(
            package, "legacy_versions", False, (replace(_version(), date=record.date),)
        ),
        publication_pending_at=record.date,
    )
    repository.mark_package_batch_completed(package, "active", record.date)
    destination = tmp_path / "index" / package.owner / package.repo / "Demo.json"
    client = _FakeClient()
    request = _request(package, destination)
    request = replace(
        request,
        since=record.date,
        batch_marker="active",
        policy=replace(request.policy, mode=mode),
    )

    result = PackageRefreshService(
        repository, client, _execution(tmp_path / "optout.txt")
    ).refresh(request)

    assert result.outcome == "refreshed"
    assert not result.package_written
    assert not repository.package_publication_pending(package)
    snapshot = repository.package_snapshot(package, since=record.date)
    assert snapshot is not None
    assert snapshot.package.record == record
    rendered = json.loads(destination.read_text(encoding="utf-8"))
    assert rendered["date"] == record.date
    assert rendered["raw_downloads"] == record.downloads
    assert rendered["raw_downloads_month"] == record.downloads_month
    assert not client.rest_requests
    assert not client.text_requests


def test_optout_cleanup_retires_data_without_removing_sibling_endpoints(
    tmp_path: Path,
) -> None:
    """Exclusion retires its data and sidecars without deleting dotted siblings."""

    package = _package()
    repository = DatabaseRepositories(DatabaseSettings(tmp_path / "index.db")).packages
    repository.write_package_pending_publication(_package_record(package))
    repository.flush_version_stage(
        VersionStage(package, "legacy_versions", False, (_version(),)),
        publication_pending_at=_TODAY,
    )
    destination = tmp_path / "index" / package.owner / package.repo / "Demo.json"
    destination.parent.mkdir(parents=True)
    destination.write_text("{}\n", encoding="utf-8")
    destination.with_suffix(".xml").write_text("<xml></xml>\n", encoding="utf-8")
    retained = (
        "Demo.worker.json",
        "Demo.worker.xml",
        "Demo.json.tmp.worker.json",
        "Demo.json.abs.xml",
        "Demo.json.rel.json",
        "Demo.xml.json",
    )
    for name in (
        *retained,
        "Demo.json.tmp123",
        "Demo.json.abs.2",
        "Demo.json.rel.worker",
    ):
        (destination.parent / name).write_text(
            "retained or transient\n", encoding="utf-8"
        )
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
    assert {path.name for path in destination.parent.iterdir()} == set(retained)


def test_failed_optout_cleanup_retains_retryable_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failed file removal retains history and pending publication until retry."""

    package = _package()
    repository = DatabaseRepositories(DatabaseSettings(tmp_path / "index.db")).packages
    repository.write_package(_package_record(package))
    destination = tmp_path / "index" / package.owner / package.repo / "Demo.json"
    destination.parent.mkdir(parents=True)
    destination.write_text("{}\n", encoding="utf-8")
    optout_file = tmp_path / "optout.txt"
    optout_file.write_text("Example/Packages/Demo\n", encoding="utf-8")
    service = PackageRefreshService(repository, _FakeClient(), _execution(optout_file))
    request = _request(package, destination)

    def deny_cleanup(_destination: Path) -> None:
        raise PermissionError("cleanup denied")

    with monkeypatch.context() as failing:
        failing.setattr(
            bkg_py.packages.updates, "remove_package_artifacts", deny_cleanup
        )
        with pytest.raises(PermissionError, match="cleanup denied"):
            service.refresh(request)

    assert repository.package_snapshot(package, since=_TODAY) is not None
    assert repository.package_publication_pending(package)
    assert destination.exists()

    assert service.refresh(request).outcome == "opted_out"
    assert repository.package_snapshot(package, since=_TODAY) is None
    assert not repository.package_publication_pending(package)
    assert not destination.exists()
