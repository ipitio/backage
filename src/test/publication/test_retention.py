"""Tests for selective database release retention."""

import httpx
import pytest

from bkg_py.github import GitHubClient, GitHubSettings
from bkg_py.publication.retention import (
    ReleaseRetentionError,
    apply_release_retention,
    plan_release_retention,
)


def _release(
    release_id: int,
    tag: str,
    *assets: tuple[int, str],
    draft: bool = False,
    prerelease: bool = False,
) -> dict[str, object]:
    return {
        "id": release_id,
        "tag_name": tag,
        "draft": draft,
        "prerelease": prerelease,
        "assets": [{"id": asset_id, "name": name} for asset_id, name in assets],
    }


def test_plan_retains_monthly_snapshots_and_every_rotation_archive() -> None:
    """Historical intervals survive while redundant live snapshots are bounded."""

    plan = plan_release_retention(
        [
            _release(80, "v2026.8.0", (800, "index.db")),
            _release(72, "v2026.7.2", (720, "index.db")),
            _release(
                71,
                "v2026.7.1",
                (710, "index.db"),
                (711, "2026.07.16T01.02.03.000004Z.index.db.zst"),
            ),
            _release(70, "v2026.7.0", (700, "index.db")),
            _release(62, "v2026.6.2", (620, "index.db")),
            _release(
                61,
                "v2026.6.1",
                (610, "index.db"),
                (611, "2026.06.16.index.db.zst"),
            ),
            _release(50, "nightly", (500, "index.db")),
            _release(49, "v2026.5.2", (490, "index.db"), prerelease=True),
            _release(48, "v2025.5.4", (480, "index.db.zst")),
        ]
    )

    assert plan.restore_release_ids == frozenset({80, 72, 62, 48})
    assert plan.archival_release_ids == frozenset({71, 61})
    assert [release.release_id for release in plan.delete_releases] == [70]
    assert [
        (release.release_id, asset.asset_id) for release, asset in plan.delete_assets
    ] == [(71, 710), (61, 610)]


def test_plan_prefers_latest_healthy_snapshot_for_an_old_month() -> None:
    """A malformed later release does not replace its month's restore point."""

    plan = plan_release_retention(
        [
            _release(80, "v2026.8.0", (800, "index.db")),
            _release(72, "v2026.7.2"),
            _release(71, "v2026.7.1", (710, "index.db")),
        ]
    )

    assert plan.restore_release_ids == frozenset({80, 71})
    assert [release.release_id for release in plan.delete_releases] == [72]


def test_managed_release_requires_valid_ids() -> None:
    """Incomplete managed metadata fails closed before deletion planning."""

    with pytest.raises(ReleaseRetentionError, match=r"release v2026\.8\.0"):
        plan_release_retention([_release(0, "v2026.8.0")])


def test_apply_release_retention_deletes_only_planned_resources() -> None:
    """The REST adapter removes redundant assets, releases, and matching tags."""

    releases = [
        _release(80, "v2026.8.0", (800, "index.db")),
        _release(72, "v2026.7.2", (720, "index.db")),
        _release(
            71,
            "v2026.7.1",
            (710, "index.db"),
            (711, "2026.07.16.index.db.zst"),
        ),
        _release(70, "v2026.7.0", (700, "index.db")),
    ]
    requests: list[tuple[str, str]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        if request.method == "GET":
            return httpx.Response(200, json=releases)
        return httpx.Response(204)

    messages: list[str] = []
    with GitHubClient(
        GitHubSettings(token="", user_agent="test-agent"),
        client=httpx.Client(transport=httpx.MockTransport(respond)),
    ) as client:
        result = apply_release_retention(
            client,
            owner="example",
            repo="bkg",
            progress=messages.append,
        )

    assert result.deleted_assets == 1
    assert result.deleted_releases == 1
    assert requests == [
        ("GET", "/repos/example/bkg/releases"),
        ("DELETE", "/repos/example/bkg/releases/assets/710"),
        ("DELETE", "/repos/example/bkg/releases/70"),
        ("DELETE", "/repos/example/bkg/git/refs/tags/v2026.7.0"),
    ]
    assert messages[0].startswith("Release retention plan: managed=4")


def test_apply_release_retention_dry_run_performs_no_deletes() -> None:
    """Dry runs expose the complete plan without mutating GitHub."""

    requests: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request.method)
        return httpx.Response(
            200,
            json=[
                _release(80, "v2026.8.0", (800, "index.db")),
                _release(70, "v2026.7.0", (700, "index.db")),
                _release(60, "v2026.7.1", (600, "index.db")),
            ],
        )

    with GitHubClient(
        GitHubSettings(token="", user_agent="test-agent"),
        client=httpx.Client(transport=httpx.MockTransport(respond)),
    ) as client:
        result = apply_release_retention(
            client,
            owner="example",
            repo="bkg",
            dry_run=True,
        )

    assert result.dry_run is True
    assert result.deleted_releases == 1
    assert requests == ["GET"]
