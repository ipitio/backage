"""Shared credential-safe Git command execution for workspace adapters."""

from __future__ import annotations

import re
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

from ..config import SettingsSnapshot
from ..runtime import resolve_executable

CREDENTIAL_HELPER = "!f() { printf '%s\\n' \"password=$GITHUB_TOKEN\"; }; f"
_URL_CREDENTIALS = re.compile(r"(https?://)[^/@\s]+@")


def redact_git_detail(detail: str, values: Iterable[str] = ()) -> str:
    """Remove known credentials and URL user information from diagnostics."""

    for value in values:
        if value:
            detail = detail.replace(value, "***")
    return _URL_CREDENTIALS.sub(r"\1***@", detail)


class WorkspaceError(RuntimeError):
    """A repository workspace operation could not be completed."""


class GitCommandRunner:  # pylint: disable=too-few-public-methods
    """Execute credential-safe Git commands for one worktree."""

    def __init__(
        self,
        path: Path,
        *,
        environment: Mapping[str, str] | None = None,
        redacted_values: Iterable[str] = (),
    ) -> None:
        self.path = path
        self._git = resolve_executable("git")
        self._environment = (
            None if environment is None else SettingsSnapshot(environment)
        )
        self._redacted_values = tuple(
            dict.fromkeys(value for value in redacted_values if value)
        )

    @property
    def command_environment(self) -> Mapping[str, str] | None:
        """Return the captured base environment for related Git adapters."""

        return self._environment

    @property
    def redacted_values(self) -> tuple[str, ...]:
        """Return values omitted from command diagnostics."""

        return self._redacted_values

    def is_worktree(self) -> bool:
        """Return whether the path belongs to a Git worktree."""

        result = self._run(("rev-parse", "--is-inside-work-tree"))
        return result.returncode == 0 and result.stdout.strip() == "true"

    def current_branch(self) -> str:
        """Return the current branch, or an empty value for detached HEAD."""

        result = self._run(("branch", "--show-current"), required=True)
        return result.stdout.strip()

    def _run(  # pylint: disable=too-many-arguments
        self,
        arguments: Sequence[str],
        *,
        environment: Mapping[str, str] | None = None,
        input_text: str | None = None,
        required: bool = False,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        try:
            command_environment = (
                None
                if self._environment is None and environment is None
                else {
                    **(self._environment or {}),
                    **(environment or {}),
                }
            )
            result = subprocess.run(  # noqa: S603
                (
                    self._git,
                    "-c",
                    f"safe.directory={self.path.resolve()}",
                    "-C",
                    str(self.path),
                    *arguments,
                ),
                check=False,
                capture_output=True,
                env=command_environment,
                input=input_text,
                shell=False,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as error:
            command = arguments[0] if arguments else "command"
            raise WorkspaceError(
                f"git {command} timed out after {timeout:g}s in {self.path}"
            ) from error
        if required and result.returncode != 0:
            self._raise_command_error(arguments, result)
        return result

    def _raise_command_error(
        self,
        arguments: Sequence[str],
        result: subprocess.CompletedProcess[str],
    ) -> None:
        detail = redact_git_detail(
            result.stderr.strip() or result.stdout.strip(),
            self._redacted_values,
        )
        message = (
            f"git {arguments[0]} failed with status {result.returncode} in {self.path}"
        )
        if detail:
            message = f"{message}: {detail}"
        raise WorkspaceError(message)
