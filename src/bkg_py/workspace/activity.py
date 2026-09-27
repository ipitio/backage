"""Record Main's daily activity only after its snapshot is uploaded."""

import argparse
import json
import re
import sys
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, cast

from ..config import (
    DEFAULT_GITHUB_OWNER,
    DEFAULT_GITHUB_REPOSITORY,
    RepositoryIdentity,
    SettingsSnapshot,
)
from ..database.maintenance.metrics import DatabaseWriteCounts
from ..database.models import PackageInventory
from ..files import atomic_text_output
from ..github import GitHubClient, GitHubError, GitHubSettings
from ..publication.release import release_tag
from ..result import ExitStatus
from ..runtime_names import DAILY_ACTIVITY_FILE, PUBLICATION_RECEIPT_FILE
from ..runtime_names import EnvironmentVariable as Env
from .git import WorkspaceError
from .publication import GitBranchPublisher
from .settings import WorkspaceSettings

_RECEIPT_LIMIT_BYTES = 16_384
_OUTCOMES = {
    ExitStatus.SUCCESS: "completed",
    ExitStatus.GRACEFUL_STOP: "graceful_stop",
}


def is_main_deployment(identity: RepositoryIdentity) -> bool:
    """Return whether daily source reporting belongs to this deployment."""

    return (
        identity.owner.casefold() == DEFAULT_GITHUB_OWNER
        and identity.name.casefold() == DEFAULT_GITHUB_REPOSITORY
        and identity.branch == "master"
    )


@dataclass(frozen=True)
class SnapshotReceipt:
    """Identity of the exact snapshot prepared by one successful run."""

    name: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class RunReceipt:
    """Bounded local evidence carried across the workflow's release upload."""

    identity: RepositoryIdentity
    data_date: date
    run_id: str
    status: ExitStatus
    snapshot: SnapshotReceipt
    catalog: PackageInventory
    observations_written: DatabaseWriteCounts

    def document(self) -> dict[str, object]:
        """Return the versioned JSON representation without credentials."""

        if self.status not in _OUTCOMES:
            raise WorkspaceError("cannot record an unsuccessful publication")
        return {
            "schema_version": 1,
            "identity": asdict(self.identity),
            "data_date": self.data_date.isoformat(),
            "run_id": self.run_id,
            "outcome": _OUTCOMES[self.status],
            "snapshot": asdict(self.snapshot),
            "catalog": asdict(self.catalog),
            "observations_written": asdict(self.observations_written),
        }


def write_run_receipt(root: Path, receipt: RunReceipt) -> None:
    """Replace the untracked receipt only after both Git branches publish."""

    _write_document(receipt_path(root), receipt.document())


def receipt_path(root: Path) -> Path:
    """Return the ignored run receipt path outside uploaded snapshot assets."""

    return root / ".bkg" / PUBLICATION_RECEIPT_FILE


def read_run_receipt(root: Path) -> RunReceipt:
    """Validate a bounded receipt before using it as publication evidence."""

    path = receipt_path(root)
    if path.stat().st_size > _RECEIPT_LIMIT_BYTES:
        raise WorkspaceError("publication receipt exceeds its byte limit")
    value = _object(json.loads(path.read_text(encoding="utf-8")))
    if _count(value.get("schema_version")) != 1:
        raise WorkspaceError("unsupported publication receipt version")
    identity = _object(value.get("identity"))
    snapshot = _object(value.get("snapshot"))
    catalog = _object(value.get("catalog"))
    writes = _object(value.get("observations_written"))
    data_date = _text(value.get("data_date"))
    parsed_date = date.fromisoformat(data_date)
    if parsed_date.isoformat() != data_date:
        raise WorkspaceError("publication receipt date must use YYYY-MM-DD")
    outcome = _text(value.get("outcome"))
    statuses = {name: status for status, name in _OUTCOMES.items()}
    if outcome not in statuses:
        raise WorkspaceError("publication receipt records an unsuccessful run")
    digest = _text(snapshot.get("sha256"))
    if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise WorkspaceError("publication receipt has an invalid snapshot digest")
    return RunReceipt(
        identity=RepositoryIdentity(
            _text(identity.get("owner")),
            _text(identity.get("name")),
            _text(identity.get("branch")),
        ),
        data_date=parsed_date,
        run_id=_text(value.get("run_id")),
        status=statuses[outcome],
        snapshot=SnapshotReceipt(
            _text(snapshot.get("name")), _count(snapshot.get("size_bytes")), digest
        ),
        catalog=PackageInventory(
            *(
                _count(catalog.get(name))
                for name in ("owners", "repositories", "packages")
            )
        ),
        observations_written=DatabaseWriteCounts(
            _count(writes.get("package_rows")), _count(writes.get("version_rows"))
        ),
    )


def publish_daily_activity(
    publisher: GitBranchPublisher,
    receipt: RunReceipt,
    release: Mapping[str, Any],
    publication_date: date,
) -> bool:
    """Publish at most one verified activity record per UTC completion day."""

    if not is_main_deployment(receipt.identity):
        return False
    if receipt.data_date > publication_date:
        raise WorkspaceError("publication receipt is dated after its completion")
    asset_id = _verify_uploaded_snapshot(receipt.snapshot, release)
    if publisher.current_branch() != "master":
        raise WorkspaceError("daily activity requires the master source branch")
    publisher.require_unstaged_index()
    publisher.synchronize()
    publisher.require_published_head()
    destination = publisher.path / DAILY_ACTIVITY_FILE
    if destination.exists():
        existing = _object(json.loads(destination.read_text(encoding="utf-8")))
        recorded_date = date.fromisoformat(_text(existing.get("publication_date")))
        if recorded_date >= publication_date:
            return False
    document = receipt.document()
    document.update(
        {
            "publication_date": publication_date.isoformat(),
            "release": {"tag": _text(release.get("tag_name")), "asset_id": asset_id},
        }
    )
    _write_document(destination, document)
    return publisher.publish(
        "master",
        f"chore(activity): record publication for {publication_date.isoformat()}",
        pathspecs=(DAILY_ACTIVITY_FILE,),
    )


def run_activity_report(args: argparse.Namespace) -> ExitStatus:
    """Verify the workflow's uploaded snapshot before recording daily activity."""

    settings = WorkspaceSettings.from_mapping(SettingsSnapshot.from_env())
    if not is_main_deployment(settings.repository):
        sys.stdout.write("Daily source activity reporting is Main-only; skipping\n")
        return ExitStatus.SUCCESS
    try:
        root = Path(args.repository).resolve()
        receipt = read_run_receipt(root)
        if (
            receipt.identity != settings.repository
            or receipt.run_id != settings.source.get(Env.GITHUB_RUN_ID)
            or receipt.data_date != args.run_date
        ):
            raise WorkspaceError("publication receipt belongs to another run")
        tag = release_tag(args.run_date)
        with GitHubClient(GitHubSettings.from_mapping(settings.source)) as client:
            release = _object(
                client.rest_json(
                    f"/repos/{settings.repository.owner}/{settings.repository.name}"
                    f"/releases/tags/{tag}"
                ).value
            )
        if release.get("tag_name") != tag:
            raise WorkspaceError("GitHub returned a different publication release")
        updated = publish_daily_activity(
            GitBranchPublisher(
                root,
                environment=settings.resolved_mapping(),
                redacted_values=settings.redacted_values(),
            ),
            receipt,
            release,
            datetime.now(UTC).date(),
        )
    except (OSError, ValueError, GitHubError, WorkspaceError) as error:
        sys.stderr.write(f"{error}\n")
        return ExitStatus.NON_FATAL
    message = (
        "Published daily source activity"
        if updated
        else "Daily activity already recorded"
    )
    sys.stdout.write(f"{message}\n")
    return ExitStatus.SUCCESS


def _verify_uploaded_snapshot(
    snapshot: SnapshotReceipt,
    release: Mapping[str, Any],
) -> int:
    if release.get("draft") is not False or release.get("prerelease") is not False:
        raise WorkspaceError("daily activity requires a published stable release")
    assets = release.get("assets")
    if not isinstance(assets, list):
        raise WorkspaceError("release has no uploaded snapshot inventory")
    matches: list[Mapping[str, Any]] = []
    for value in cast(list[object], assets):
        asset = _object(value)
        if asset.get("name") == snapshot.name:
            matches.append(asset)
    if len(matches) != 1:
        raise WorkspaceError("release must contain exactly one matching snapshot")
    asset = matches[0]
    if (
        asset.get("state") != "uploaded"
        or _count(asset.get("size")) != snapshot.size_bytes
        or snapshot.size_bytes <= 0
        or asset.get("digest") != f"sha256:{snapshot.sha256}"
    ):
        raise WorkspaceError("uploaded snapshot does not match the run receipt")
    return _count(asset.get("id"))


def _write_document(path: Path, document: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with atomic_text_output(path) as output:
        json.dump(document, output, indent=2, sort_keys=True)
        output.write("\n")


def _object(value: object) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise WorkspaceError("publication evidence must contain a JSON object")
    return cast(Mapping[str, Any], value)


def _text(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise WorkspaceError("publication evidence has a missing text field")
    return value


def _count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise WorkspaceError("publication evidence has an invalid count")
    return value
