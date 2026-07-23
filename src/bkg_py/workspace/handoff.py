"""Isolated Git control-ref signaling for graceful workflow handoff."""

from __future__ import annotations

import os
import threading
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from ..runtime import StopController
from .repository import GitControlRefRepository, WorkspaceError

MessageSink = Callable[[str], None]
_FORMAT_MARKER = "Bkg-Control-Format: isolated-v1"
_MISSING_BASELINE = "missing"
_REQUEST_ATTEMPTS = 3


def _discard_message(_message: str) -> None:
    return


def _positive_float(value: str | None, default: float) -> float:
    if value is None:
        return default
    try:
        parsed = float(value)
    except ValueError:
        return default
    return parsed if parsed > 0 else default


@dataclass(frozen=True)
class HandoffSettings:
    """Control-ref settings shared by requesters and active-run monitors."""

    control_ref: str
    poll_seconds: float = 60
    git_timeout_seconds: float = 20

    @classmethod
    def from_env(cls) -> HandoffSettings:
        """Read handoff settings from the workflow environment."""

        return cls(
            control_ref=os.environ.get("BKG_HANDOFF_CONTROL_REF", ""),
            poll_seconds=_positive_float(
                os.environ.get("BKG_HANDOFF_POLL_SECONDS"),
                60,
            ),
            git_timeout_seconds=_positive_float(
                os.environ.get("BKG_HANDOFF_GIT_TIMEOUT_SECONDS"),
                20,
            ),
        )

    def validated_ref(self) -> str:
        """Return a branch ref suitable for remote control signaling."""

        if not self.control_ref.startswith("refs/heads/"):
            raise WorkspaceError(
                "BKG_HANDOFF_CONTROL_REF must name a branch under refs/heads"
            )
        return self.control_ref


class WorkflowHandoffControl:
    """Read, advance, and monitor an isolated workflow control ref."""

    def __init__(
        self,
        repository: Path | GitControlRefRepository,
        settings: HandoffSettings,
        *,
        progress: MessageSink | None = None,
        diagnostic: MessageSink | None = None,
    ) -> None:
        self.repository = (
            repository
            if isinstance(repository, GitControlRefRepository)
            else GitControlRefRepository(repository)
        )
        self.settings = settings
        self.progress = progress or _discard_message
        self.diagnostic = diagnostic or _discard_message

    def current_baseline(self) -> str:
        """Return the current remote control SHA or the missing-ref marker."""

        ref = self.settings.validated_ref()
        sha = self.repository.remote_ref_sha(
            ref,
            timeout=self.settings.git_timeout_seconds,
        )
        return sha or _MISSING_BASELINE

    def capture_baseline(self) -> str | None:
        """Capture a monitor baseline, disabling handoff on transport failure."""

        if not self.settings.control_ref:
            return None
        try:
            return self.current_baseline()
        except WorkspaceError:
            self.diagnostic(
                "Failed to capture workflow handoff baseline; "
                "handoff disabled for this run"
            )
            return None

    def request(self) -> None:  # noqa: C901
        """Advance the control ref with bounded compare-and-swap retries."""

        ref = self.settings.validated_ref()
        for attempt in range(1, _REQUEST_ATTEMPTS + 1):
            try:
                remote_sha = self.repository.remote_ref_sha(
                    ref,
                    timeout=self.settings.git_timeout_seconds,
                )
            except WorkspaceError as error:
                raise WorkspaceError("Failed to read workflow handoff ref") from error

            if remote_sha is None:
                candidate = self._create_commit()
                if self.repository.push_ref(candidate, ref):
                    self._report_requested()
                    return
                self._report_race(attempt)
                continue

            try:
                base = self.repository.fetch_ref(
                    ref,
                    timeout=self.settings.git_timeout_seconds,
                )
            except WorkspaceError:
                self._report_race(attempt)
                continue

            if self._tip_is_isolated(base):
                candidate = self._create_commit(parent=base)
                if self.repository.push_ref(candidate, ref):
                    self._report_requested()
                    return
                self._report_race(attempt)
                continue

            candidate = self._create_commit()
            if self.repository.push_ref(
                candidate,
                ref,
                force_with_lease=remote_sha,
            ):
                self.progress("Migrated workflow handoff ref to isolated history")
                self._report_requested()
                return

            current_sha = self.repository.remote_ref_sha(
                ref,
                timeout=self.settings.git_timeout_seconds,
            )
            if current_sha != remote_sha:
                self._report_race(attempt)
                continue

            candidate = self._create_commit(parent=base, isolated=False)
            if self.repository.push_ref(candidate, ref):
                self.diagnostic(
                    "Workflow handoff ref could not be isolated; "
                    "preserving its existing history"
                )
                self._report_requested()
                return
            self._report_race(attempt)

        raise WorkspaceError(
            f"Failed to request workflow handoff after {_REQUEST_ATTEMPTS} attempts"
        )

    @contextmanager
    def monitor(
        self,
        baseline: str | None,
        stop: StopController,
    ) -> Generator[None]:
        """Monitor a captured baseline for the lifetime of an active run."""

        if baseline is None:
            yield
            return

        finished = threading.Event()
        monitor = threading.Thread(
            target=self._monitor,
            args=(baseline, stop, finished),
            name="bkg-handoff-monitor",
            daemon=True,
        )
        monitor.start()
        try:
            yield
        finally:
            finished.set()
            monitor.join()

    def _monitor(
        self,
        baseline: str,
        stop: StopController,
        finished: threading.Event,
    ) -> None:
        reported_failure = False
        while not finished.is_set():
            try:
                current = self.current_baseline()
            except WorkspaceError:
                if not reported_failure:
                    self.diagnostic(
                        "Failed to check workflow handoff ref; "
                        "the active update will continue"
                    )
                    reported_failure = True
            else:
                reported_failure = False
                if current != baseline:
                    self.progress(
                        "Workflow handoff requested; stopping gracefully "
                        "before the next publication"
                    )
                    stop.request_stop("handoff")
                    return
            finished.wait(self.settings.poll_seconds)

    def _tip_is_isolated(self, commit: str) -> bool:
        return (
            self.repository.commit_tree_id(commit) == self.repository.empty_tree()
            and _FORMAT_MARKER in self.repository.commit_message(commit)
        )

    def _create_commit(
        self,
        *,
        parent: str | None = None,
        isolated: bool = True,
    ) -> str:
        actor = os.environ.get("GITHUB_ACTOR") or "github-actions[bot]"
        email_actor = os.environ.get("GITHUB_ACTOR") or "41898282+github-actions[bot]"
        identity = {
            "GIT_AUTHOR_NAME": actor,
            "GIT_AUTHOR_EMAIL": f"{email_actor}@users.noreply.github.com",
            "GIT_COMMITTER_NAME": actor,
            "GIT_COMMITTER_EMAIL": f"{email_actor}@users.noreply.github.com",
        }
        return self.repository.commit_tree(
            f"Request workflow handoff ({os.environ.get('GITHUB_RUN_ID', 'manual')})",
            parent=parent,
            additional_message=_FORMAT_MARKER if isolated else None,
            environment=identity,
        )

    def _report_requested(self) -> None:
        self.progress("Requested graceful handoff from the active update")

    def _report_race(self, attempt: int) -> None:
        self.diagnostic(
            "Workflow handoff ref changed concurrently; "
            f"retrying ({attempt}/{_REQUEST_ATTEMPTS})"
        )
