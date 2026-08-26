"""Isolated Git control-ref signaling for graceful workflow handoff."""

import threading
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import cast

from ..runtime import StopController
from .git import GitCommandRunner, WorkspaceError
from .settings import HandoffSettings

MessageSink = Callable[[str], None]
_FORMAT_MARKER = "Bkg-Control-Format: isolated-v1"
_MISSING_BASELINE = "missing"
_REQUEST_ATTEMPTS = 3


def _discard_message(_message: str) -> None:
    return


class GitControlRefRepository(GitCommandRunner):
    """Read and mutate one exact remote control ref without checkout changes."""

    def remote_ref_sha(
        self,
        ref: str,
        *,
        remote: str = "origin",
        timeout: float | None = None,
    ) -> str | None:
        """Return one exact remote ref SHA, or None when the ref is absent."""

        result = self._run(
            ("ls-remote", "--refs", remote, ref),
            required=True,
            timeout=timeout,
        )
        first_line = next(iter(result.stdout.splitlines()), "")
        sha, _separator, _name = first_line.partition("\t")
        return sha or None

    def fetch_ref(
        self,
        ref: str,
        *,
        remote: str = "origin",
        timeout: float | None = None,
    ) -> str:
        """Fetch one exact ref and return the fetched commit SHA."""

        self._run(
            ("fetch", "--quiet", "--no-tags", "--depth=1", remote, ref),
            required=True,
            timeout=timeout,
        )
        return self._run(
            ("rev-parse", "FETCH_HEAD"),
            required=True,
        ).stdout.strip()

    def empty_tree(self) -> str:
        """Return Git's canonical empty-tree object ID."""

        return self._run(("mktree",), input_text="", required=True).stdout.strip()

    def commit_tree(
        self,
        message: str,
        *,
        parent: str | None = None,
        additional_message: str | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> str:
        """Create an empty-tree commit without changing the worktree."""

        arguments = ["commit-tree", self.empty_tree()]
        if parent is not None:
            arguments.extend(("-p", parent))
        arguments.extend(("-m", message))
        if additional_message is not None:
            arguments.extend(("-m", additional_message))
        return self._run(
            arguments,
            environment=environment,
            required=True,
        ).stdout.strip()

    def commit_tree_id(self, commit: str) -> str:
        """Return the tree object ID referenced by a commit."""

        return self._run(
            ("show", "-s", "--format=%T", commit),
            required=True,
        ).stdout.strip()

    def commit_message(self, commit: str) -> str:
        """Return a commit's complete message."""

        return self._run(
            ("show", "-s", "--format=%B", commit),
            required=True,
        ).stdout

    def push_ref(
        self,
        commit: str,
        ref: str,
        *,
        remote: str = "origin",
        force_with_lease: str | None = None,
    ) -> bool:
        """Try to push one commit to a ref, returning False for a rejected race."""

        arguments = ["push", "--quiet"]
        if force_with_lease is not None:
            arguments.append(f"--force-with-lease={ref}:{force_with_lease}")
        arguments.extend((remote, f"{commit}:{ref}"))
        return self._run(arguments).returncode == 0


def scheduled_update_skip_reason(
    queued_baseline: str,
    current_baseline: str,
    run_id: str,
    latest_scheduled_run_id: str,
    active_manual_run_id: str,
) -> str | None:
    """Return why a serialized scheduled update should yield to newer work."""

    if current_baseline != queued_baseline:
        return (
            "Skipping scheduled update: a Manual handoff was requested "
            "after this run queued"
        )
    if active_manual_run_id:
        return (
            f"Skipping scheduled update: Manual run {active_manual_run_id} is waiting"
        )
    if (
        run_id.isdigit()
        and latest_scheduled_run_id.isdigit()
        and int(latest_scheduled_run_id) > int(run_id)
    ):
        return (
            "Skipping scheduled update: scheduled run "
            f"{latest_scheduled_run_id} supersedes {run_id}"
        )
    return None


def workflow_run_freshness(data: object) -> tuple[str, str]:
    """Select the newest scheduled and waiting Manual run IDs."""

    if not isinstance(data, dict):
        raise ValueError("workflow runs response must be an object")
    response = cast(dict[str, object], data)
    raw_runs = response.get("workflow_runs")
    if not isinstance(raw_runs, list):
        raise ValueError("workflow runs response is missing workflow_runs")
    runs = cast(list[object], raw_runs)

    scheduled: list[int] = []
    manual: list[int] = []
    for raw_item in runs:
        if not isinstance(raw_item, dict):
            continue
        item = cast(dict[str, object], raw_item)
        run_id = item.get("id")
        path = item.get("path")
        if not isinstance(run_id, int) or isinstance(run_id, bool):
            continue
        if (
            item.get("event") == "schedule"
            and isinstance(path, str)
            and path.endswith("/update.yml")
        ):
            scheduled.append(run_id)
        if (
            isinstance(path, str)
            and path.endswith("/manual.yml")
            and item.get("status") != "completed"
        ):
            manual.append(run_id)
    return (
        str(max(scheduled)) if scheduled else "",
        str(max(manual)) if manual else "",
    )


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

        ref = self._validated_ref()
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

    def request(self) -> None:
        """Advance the control ref with bounded compare-and-swap retries."""

        ref = self._validated_ref()
        for attempt in range(1, _REQUEST_ATTEMPTS + 1):
            if self._request_once(ref):
                return
            self._report_race(attempt)

        raise WorkspaceError(
            f"Failed to request workflow handoff after {_REQUEST_ATTEMPTS} attempts"
        )

    def _validated_ref(self) -> str:
        if not self.settings.control_ref.startswith("refs/heads/"):
            raise WorkspaceError(
                "BKG_HANDOFF_CONTROL_REF must name a branch under refs/heads"
            )
        return self.settings.control_ref

    def _request_once(self, ref: str) -> bool:
        try:
            remote_sha = self.repository.remote_ref_sha(
                ref,
                timeout=self.settings.git_timeout_seconds,
            )
        except WorkspaceError as error:
            raise WorkspaceError("Failed to read workflow handoff ref") from error

        if remote_sha is None:
            return self._push_isolated_request(ref, previous_sha=None)

        try:
            base = self.repository.fetch_ref(
                ref,
                timeout=self.settings.git_timeout_seconds,
            )
        except WorkspaceError:
            return False

        if self._tip_is_isolated(base):
            return self._push_isolated_request(
                ref,
                previous_sha=remote_sha,
                parent=base,
            )
        return self._migrate_legacy_request(ref, remote_sha, base)

    def _push_isolated_request(
        self,
        ref: str,
        *,
        previous_sha: str | None,
        parent: str | None = None,
        migrate: bool = False,
    ) -> bool:
        candidate = self._create_commit(parent=parent)
        force_with_lease = previous_sha if migrate else None
        if self.repository.push_ref(
            candidate,
            ref,
            force_with_lease=force_with_lease,
        ):
            if migrate:
                self.progress("Migrated workflow handoff ref to isolated history")
            self._report_requested()
            return True
        return self._request_completed_concurrently(ref, previous_sha)

    def _migrate_legacy_request(
        self,
        ref: str,
        remote_sha: str,
        base: str,
    ) -> bool:
        if self._push_isolated_request(
            ref,
            previous_sha=remote_sha,
            migrate=True,
        ):
            return True

        candidate = self._create_commit(parent=base, isolated=False)
        if self.repository.push_ref(candidate, ref):
            self.diagnostic(
                "Workflow handoff ref could not be isolated; "
                "preserving its existing history"
            )
            self._report_requested()
            return True
        return self._request_completed_concurrently(ref, remote_sha)

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
        return self.repository.commit_tree(
            f"Request workflow handoff ({self.settings.run_id})",
            parent=parent,
            additional_message=_FORMAT_MARKER if isolated else None,
            environment=self.settings.identity.environment(),
        )

    def _report_requested(self) -> None:
        self.progress("Requested graceful handoff from the active update")

    def _request_completed_concurrently(
        self,
        ref: str,
        previous_sha: str | None,
    ) -> bool:
        try:
            current_sha = self.repository.remote_ref_sha(
                ref,
                timeout=self.settings.git_timeout_seconds,
            )
        except WorkspaceError:
            return False
        if current_sha == previous_sha:
            return False
        self.progress("Graceful handoff was already requested concurrently")
        return True

    def _report_race(self, attempt: int) -> None:
        self.diagnostic(
            "Workflow handoff ref changed concurrently; "
            f"retrying ({attempt}/{_REQUEST_ATTEMPTS})"
        )
