"""Tests for workflow commands that run without installed Python packages."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from .repository_support import clone_repository, create_repository_with_remote, git

_SOURCE_DIRECTORY = Path(__file__).parents[2]
_CONTROL_REF = "refs/heads/bkg-control"


@pytest.mark.parametrize(
    ("arguments", "expected_output"),
    [
        (("handoff", "baseline", "{repository}"), "missing\n"),
        (
            ("handoff", "request", "{repository}"),
            "Requested graceful handoff from the active update\n",
        ),
        (("handoff", "should-run", "missing", "missing", "100", "100"), ""),
        (("handoff", "workflow-runs"), "|\n"),
        (
            ("configure-fork-merge", "{repository}"),
            "Updated fork-local merge handling",
        ),
        (
            (
                "workflow-sync-fork",
                "-C",
                "{repository}",
                "-u",
                "{upstream}",
                "-b",
                "master",
            ),
            "updated=false\n",
        ),
    ],
    ids=(
        "handoff-baseline",
        "handoff-request",
        "handoff-should-run",
        "handoff-workflow-runs",
        "configure-fork-merge",
        "workflow-sync-fork",
    ),
)
def test_workspace_commands_run_without_installed_dependencies(
    tmp_path: Path,
    arguments: tuple[str, ...],
    expected_output: str,
) -> None:
    """Exclude site packages while exercising the actual workflow CLI paths."""

    upstream, remote = create_repository_with_remote(tmp_path)
    repository = tmp_path / "checkout"
    clone_repository(remote, repository)
    command = tuple(
        argument.format(repository=repository, upstream=upstream)
        for argument in arguments
    )

    result = subprocess.run(  # noqa: S603
        (sys.executable, "-S", "-m", "bkg_py", *command),
        cwd=repository,
        env={
            **os.environ,
            "PYTHONPATH": str(_SOURCE_DIRECTORY),
            "BKG_HANDOFF_CONTROL_REF": _CONTROL_REF,
            "GITHUB_ACTOR": "test",
            "GITHUB_RUN_ID": "123",
        },
        input='{"workflow_runs": []}\n',
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )

    assert result.returncode == 0, result.stderr
    if expected_output:
        assert expected_output in result.stdout
    else:
        assert result.stdout == ""
    if command[:2] == ("handoff", "request"):
        assert (
            _CONTROL_REF
            in git(repository, "ls-remote", "--refs", "origin", _CONTROL_REF).stdout
        )
