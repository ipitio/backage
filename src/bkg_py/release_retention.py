"""Selective retention for database snapshot and rotation releases."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import cast
from urllib.parse import quote

from .github import GitHubClient

_RELEASE_TAG = re.compile(
    r"v(?P<year>[0-9]{4})\.(?P<month>[1-9]|1[0-2])\.(?P<period>[0-9]+)"
)
_ROTATION_ARCHIVE = re.compile(
    r"[0-9]{4}\.[0-9]{2}\.[0-9]{2}"
    r"(?:T[0-9]{2}\.[0-9]{2}\.[0-9]{2}\.[0-9]{6}Z)?"
    r"(?:\.[1-9][0-9]*)?\..+\.db\.zst"
)
_RESTORE_ASSET_NAMES = frozenset({"index.db", "index.db.zst", "index.sql.zst"})
_CURRENT_DATABASE_ASSET = "index.db"


class ReleaseRetentionError(RuntimeError):
    """Release metadata cannot be cleaned without risking retained data."""


@dataclass(frozen=True)
class ReleaseAsset:
    """A release asset relevant to retention decisions."""

    asset_id: int
    name: str


@dataclass(frozen=True)
class ManagedRelease:
    """A published Bkg database release with its parsed period."""

    release_id: int
    tag: str
    year: int
    month: int
    period: int
    assets: tuple[ReleaseAsset, ...]

    @property
    def period_key(self) -> tuple[int, int, int]:
        """Return the sortable release period."""

        return self.year, self.month, self.period

    @property
    def month_key(self) -> tuple[int, int]:
        """Return the release's calendar month."""

        return self.year, self.month

    @property
    def has_restore_snapshot(self) -> bool:
        """Return whether this release can restore the live database."""

        return any(asset.name in _RESTORE_ASSET_NAMES for asset in self.assets)

    @property
    def rotation_archives(self) -> tuple[ReleaseAsset, ...]:
        """Return historical interval archives carried by this release."""

        return tuple(
            asset for asset in self.assets if _ROTATION_ARCHIVE.fullmatch(asset.name)
        )


@dataclass(frozen=True)
class ReleaseRetentionPlan:
    """A deterministic set of release and redundant-asset deletions."""

    managed: tuple[ManagedRelease, ...]
    restore_release_ids: frozenset[int]
    archival_release_ids: frozenset[int]
    delete_releases: tuple[ManagedRelease, ...]
    delete_assets: tuple[tuple[ManagedRelease, ReleaseAsset], ...]


@dataclass(frozen=True)
class ReleaseRetentionResult:
    """Counts produced by one release-retention pass."""

    managed: int
    restore_releases: int
    archival_releases: int
    deleted_releases: int
    deleted_assets: int
    dry_run: bool


def release_has_rotation_archive(release: object) -> bool:
    """Return whether release metadata carries a historical interval archive."""

    if not isinstance(release, Mapping):
        return False
    assets = cast(Mapping[str, object], release).get("assets")
    if not isinstance(assets, list):
        return False
    return any(
        isinstance(asset, Mapping)
        and isinstance((name := cast(Mapping[str, object], asset).get("name")), str)
        and _ROTATION_ARCHIVE.fullmatch(name) is not None
        for asset in cast(list[object], assets)
    )


def plan_release_retention(releases: object) -> ReleaseRetentionPlan:
    """Plan cleanup while preserving restore points and historical intervals."""

    managed = _managed_releases(releases)
    if not managed:
        return ReleaseRetentionPlan((), frozenset(), frozenset(), (), ())

    by_month: dict[tuple[int, int], list[ManagedRelease]] = defaultdict(list)
    for release in managed:
        by_month[release.month_key].append(release)

    newest_month = max(by_month)
    restore_release_ids = {release.release_id for release in by_month[newest_month]}
    for month, monthly_releases in by_month.items():
        if month == newest_month:
            continue
        restore_release_ids.add(_monthly_restore_release(monthly_releases).release_id)

    archival_release_ids = {
        release.release_id for release in managed if release.rotation_archives
    }
    delete_releases = tuple(
        release
        for release in managed
        if release.release_id not in restore_release_ids
        and release.release_id not in archival_release_ids
    )
    delete_assets = tuple(
        (release, asset)
        for release in managed
        if release.release_id in archival_release_ids
        and release.release_id not in restore_release_ids
        for asset in release.assets
        if asset.name == _CURRENT_DATABASE_ASSET
    )
    return ReleaseRetentionPlan(
        managed,
        frozenset(restore_release_ids),
        frozenset(archival_release_ids),
        delete_releases,
        delete_assets,
    )


def apply_release_retention(
    client: GitHubClient,
    *,
    owner: str,
    repo: str,
    dry_run: bool = False,
    progress: Callable[[str], None] = lambda _message: None,
) -> ReleaseRetentionResult:
    """Apply the selective release policy through GitHub's REST API."""

    if not owner or not repo:
        raise ReleaseRetentionError("GitHub owner and repository are required")
    releases: list[object] = []
    for page in client.rest_pages(
        f"repos/{quote(owner, safe='')}/{quote(repo, safe='')}/releases?per_page=100"
    ):
        page_value: object = page.value
        if not isinstance(page_value, list):
            raise ReleaseRetentionError("release list response is not an array")
        releases.extend(cast(list[object], page_value))

    plan = plan_release_retention(releases)
    progress(
        "Release retention plan: "
        f"managed={len(plan.managed)} "
        f"restore={len(plan.restore_release_ids)} "
        f"archival={len(plan.archival_release_ids)} "
        f"delete_releases={len(plan.delete_releases)} "
        f"delete_assets={len(plan.delete_assets)} "
        f"dry_run={str(dry_run).lower()}"
    )
    for release, asset in plan.delete_assets:
        progress(f"Removing redundant {asset.name} from {release.tag}")
        if not dry_run:
            client.rest_delete(
                f"repos/{quote(owner, safe='')}/{quote(repo, safe='')}"
                f"/releases/assets/{asset.asset_id}"
            )
    for release in plan.delete_releases:
        progress(f"Deleting redundant database release {release.tag}")
        if dry_run:
            continue
        client.rest_delete(
            f"repos/{quote(owner, safe='')}/{quote(repo, safe='')}"
            f"/releases/{release.release_id}"
        )
        client.rest_delete(
            f"repos/{quote(owner, safe='')}/{quote(repo, safe='')}"
            f"/git/refs/tags/{quote(release.tag, safe='')}"
        )
    return ReleaseRetentionResult(
        managed=len(plan.managed),
        restore_releases=len(plan.restore_release_ids),
        archival_releases=len(plan.archival_release_ids),
        deleted_releases=len(plan.delete_releases),
        deleted_assets=len(plan.delete_assets),
        dry_run=dry_run,
    )


def _managed_releases(releases: object) -> tuple[ManagedRelease, ...]:
    if not isinstance(releases, list):
        raise ReleaseRetentionError("release metadata is not an array")
    managed: list[ManagedRelease] = []
    for value in cast(list[object], releases):
        if not isinstance(value, Mapping):
            continue
        metadata = cast(Mapping[str, object], value)
        tag = metadata.get("tag_name")
        if not isinstance(tag, str):
            continue
        match = _RELEASE_TAG.fullmatch(tag)
        if (
            match is None
            or metadata.get("draft") is True
            or metadata.get("prerelease") is True
        ):
            continue
        release_id = _positive_id(metadata.get("id"), f"release {tag}")
        assets = metadata.get("assets")
        if not isinstance(assets, list):
            raise ReleaseRetentionError(f"release {tag} assets are not an array")
        parsed_assets = tuple(
            asset
            for item in cast(list[object], assets)
            if (asset := _release_asset(item, tag)) is not None
        )
        managed.append(
            ManagedRelease(
                release_id=release_id,
                tag=tag,
                year=int(match.group("year")),
                month=int(match.group("month")),
                period=int(match.group("period")),
                assets=parsed_assets,
            )
        )
    return tuple(sorted(managed, key=lambda release: release.period_key, reverse=True))


def _release_asset(value: object, tag: str) -> ReleaseAsset | None:
    if not isinstance(value, Mapping):
        return None
    metadata = cast(Mapping[str, object], value)
    name = metadata.get("name")
    if not isinstance(name, str) or not name:
        return None
    return ReleaseAsset(
        asset_id=_positive_id(metadata.get("id"), f"asset {name} on {tag}"),
        name=name,
    )


def _positive_id(value: object, description: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ReleaseRetentionError(f"{description} has no valid ID")
    return value


def _monthly_restore_release(
    releases: list[ManagedRelease],
) -> ManagedRelease:
    healthy = [release for release in releases if release.has_restore_snapshot]
    return max(healthy or releases, key=lambda release: release.period_key)
