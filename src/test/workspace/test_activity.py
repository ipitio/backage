"""Daily source reporting after durable snapshot publication."""

import json
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from bkg_py.cli import main
from bkg_py.config import RepositoryIdentity
from bkg_py.database.maintenance.metrics import DatabaseWriteCounts
from bkg_py.database.models import PackageInventory
from bkg_py.result import ExitStatus
from bkg_py.runtime_names import DAILY_ACTIVITY_FILE
from bkg_py.workspace.activity import (
    RunReceipt,
    SnapshotReceipt,
    publish_daily_activity,
    read_run_receipt,
    receipt_path,
    write_run_receipt,
)
from bkg_py.workspace.git import WorkspaceError
from bkg_py.workspace.publication import GitBranchPublisher

from .repository_support import clone_repository, create_repository_with_remote, git


@pytest.fixture(name="receipt")
def run_receipt() -> RunReceipt:
    """A successful snapshot with no new observations still has real activity."""

    return RunReceipt(
        RepositoryIdentity("ipitio", "backage", "master"),
        date(2026, 9, 27),
        "12345",
        ExitStatus.SUCCESS,
        SnapshotReceipt("index.db", 8192, "a" * 64),
        PackageInventory(10, 20, 30),
        DatabaseWriteCounts(0, 0),
    )


def _release(receipt: RunReceipt) -> dict[str, object]:
    return {
        "tag_name": "2026.09.21",
        "draft": False,
        "prerelease": False,
        "assets": [
            {
                "id": 42,
                "name": receipt.snapshot.name,
                "state": "uploaded",
                "size": receipt.snapshot.size_bytes,
                "digest": f"sha256:{receipt.snapshot.sha256}",
            }
        ],
    }


@pytest.mark.parametrize("status", [ExitStatus.SUCCESS, ExitStatus.GRACEFUL_STOP])
def test_receipt_round_trip(
    tmp_path: Path, receipt: RunReceipt, status: ExitStatus
) -> None:
    """Receipts survive the workflow boundary without snapshot payload copies."""

    receipt = replace(receipt, status=status)
    write_run_receipt(tmp_path, receipt)
    assert read_run_receipt(tmp_path) == receipt
    assert receipt_path(tmp_path).parent == tmp_path / ".bkg"
    assert not (tmp_path / ".snapshot").exists()


@pytest.mark.parametrize(
    ("field", "value"),
    [("schema_version", True), ("outcome", "failed"), ("data_date", "2026-9-27")],
)
def test_invalid_receipt_is_rejected(
    tmp_path: Path, receipt: RunReceipt, field: str, value: object
) -> None:
    """Malformed and unsuccessful evidence never reaches publication."""

    document = receipt.document()
    document[field] = value
    path = receipt_path(tmp_path)
    path.parent.mkdir()
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises((ValueError, WorkspaceError)):
        read_run_receipt(tmp_path)


def test_oversized_receipt_is_rejected(tmp_path: Path) -> None:
    """A receipt is bounded before decoding untrusted JSON."""

    path = receipt_path(tmp_path)
    path.parent.mkdir()
    path.write_text(" " * 16_385, encoding="utf-8")
    with pytest.raises(WorkspaceError, match="byte limit"):
        read_run_receipt(tmp_path)


def test_daily_publication_deduplicates_remote_and_rolls_over(
    tmp_path: Path, receipt: RunReceipt
) -> None:
    """Zero-write runs commit once per completion day across separate checkouts."""

    repository, remote = create_repository_with_remote(tmp_path)
    other = tmp_path / "other"
    clone_repository(remote, other)
    git(other, "config", "user.name", "test")
    git(other, "config", "user.email", "test@example.com")
    publisher = GitBranchPublisher(repository)
    initial = git(remote, "rev-parse", "master").stdout
    assert publish_daily_activity(
        publisher, receipt, _release(receipt), receipt.data_date
    )
    first = git(remote, "rev-parse", "master").stdout
    assert first != initial
    assert not publish_daily_activity(
        GitBranchPublisher(other), receipt, _release(receipt), receipt.data_date
    )
    assert git(remote, "rev-parse", "master").stdout == first
    next_day = date(2026, 9, 28)
    assert publish_daily_activity(
        publisher,
        replace(receipt, status=ExitStatus.GRACEFUL_STOP),
        _release(receipt),
        next_day,
    )
    document = json.loads(git(remote, "show", f"master:{DAILY_ACTIVITY_FILE}").stdout)
    assert document["publication_date"] == next_day.isoformat()
    assert document["data_date"] == receipt.data_date.isoformat()
    assert document["observations_written"] == {"package_rows": 0, "version_rows": 0}
    assert document["outcome"] == "graceful_stop"
    assert document["release"]["asset_id"] == 42
    assert git(
        remote, "diff-tree", "--no-commit-id", "--name-only", "-r", "master"
    ).stdout.splitlines() == [DAILY_ACTIVITY_FILE]


def test_failed_push_cannot_be_mistaken_for_published_activity(
    tmp_path: Path, receipt: RunReceipt
) -> None:
    """A rejected report remains unpublished even though its local commit exists."""

    repository, remote = create_repository_with_remote(tmp_path)
    hook = remote / "hooks/pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    publisher = GitBranchPublisher(repository)
    before = git(remote, "rev-parse", "master").stdout
    with pytest.raises(WorkspaceError, match="push failed"):
        publish_daily_activity(publisher, receipt, _release(receipt), receipt.data_date)
    assert (repository / DAILY_ACTIVITY_FILE).exists()
    assert git(remote, "rev-parse", "master").stdout == before
    hook.unlink()
    with pytest.raises(WorkspaceError, match="fully published"):
        publish_daily_activity(publisher, receipt, _release(receipt), receipt.data_date)
    fresh = tmp_path / "fresh"
    clone_repository(remote, fresh)
    git(fresh, "config", "user.name", "test")
    git(fresh, "config", "user.email", "test@example.com")
    assert publish_daily_activity(
        GitBranchPublisher(fresh), receipt, _release(receipt), receipt.data_date
    )


@pytest.mark.parametrize("case", ["missing", "pending", "size", "digest", "draft"])
def test_unverified_upload_does_not_commit(
    tmp_path: Path, receipt: RunReceipt, case: str
) -> None:
    """A successful Git update alone is not proof of a durable release upload."""

    repository, remote = create_repository_with_remote(tmp_path)
    release = _release(receipt)
    asset: dict[str, object] = {
        "id": 42,
        "name": receipt.snapshot.name,
        "state": "uploaded",
        "size": receipt.snapshot.size_bytes,
        "digest": f"sha256:{receipt.snapshot.sha256}",
    }
    release["assets"] = [asset]
    if case == "missing":
        release["assets"] = []
    elif case == "pending":
        asset["state"] = "new"
    elif case == "size":
        asset["size"] = 0
    elif case == "digest":
        asset["digest"] = "sha256:" + "b" * 64
    else:
        release["draft"] = True
    before = git(remote, "rev-parse", "master").stdout
    with pytest.raises(WorkspaceError):
        publish_daily_activity(
            GitBranchPublisher(repository), receipt, release, receipt.data_date
        )
    assert git(remote, "rev-parse", "master").stdout == before
    assert not (repository / DAILY_ACTIVITY_FILE).exists()


@pytest.mark.parametrize("case", ["staged", "unpublished", "wrong_branch"])
def test_unrelated_git_state_is_not_published(
    tmp_path: Path, receipt: RunReceipt, case: str
) -> None:
    """Managed publication cannot accidentally carry another local change."""

    repository, remote = create_repository_with_remote(tmp_path)
    if case == "wrong_branch":
        git(repository, "switch", "-qc", "other")
    else:
        (repository / "unrelated.txt").write_text("keep me\n", encoding="utf-8")
        git(repository, "add", "unrelated.txt")
        if case == "unpublished":
            git(repository, "commit", "-qm", "unpublished source")
    before = git(remote, "rev-parse", "master").stdout
    with pytest.raises(WorkspaceError):
        publish_daily_activity(
            GitBranchPublisher(repository),
            receipt,
            _release(receipt),
            receipt.data_date,
        )
    assert git(remote, "rev-parse", "master").stdout == before
    assert not (repository / DAILY_ACTIVITY_FILE).exists()


def test_report_on_fork_needs_neither_receipt_nor_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Forks do not publish Main's daily activity record."""

    monkeypatch.setenv("GITHUB_OWNER", "Scibent")
    monkeypatch.setenv("GITHUB_REPO", "backage")
    monkeypatch.setenv("GITHUB_BRANCH", "master")
    assert (
        main(["workflow-report", "-C", str(tmp_path), "-D", "2026-09-27"])
        is ExitStatus.SUCCESS
    )
    assert "Main-only; skipping" in capsys.readouterr().out
    assert not list(tmp_path.iterdir())


def test_report_rejects_another_run_before_network(
    tmp_path: Path,
    receipt: RunReceipt,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A stale receipt cannot claim activity for an unsuccessful later run."""

    write_run_receipt(tmp_path, receipt)
    monkeypatch.setenv("GITHUB_OWNER", "ipitio")
    monkeypatch.setenv("GITHUB_REPO", "backage")
    monkeypatch.setenv("GITHUB_BRANCH", "master")
    monkeypatch.setenv("GITHUB_RUN_ID", "different")
    assert (
        main(["workflow-report", "-C", str(tmp_path), "-D", "2026-09-27"])
        is ExitStatus.NON_FATAL
    )
    assert "another run" in capsys.readouterr().err
