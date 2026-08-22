"""Outer repository, snapshot, run, and publication workflow lifecycle."""

from __future__ import annotations

import shutil
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit

from ..application import ApplicationContext
from ..config import DEFAULT_GITHUB_OWNER, SettingsSnapshot
from ..github import GitHubClient, GitHubError
from ..result import ExitStatus
from ..runtime import GracefulStop, resolve_executable
from ..runtime_names import EnvironmentVariable as Env
from ..runtime_names import StateKey
from ..snapshots import SnapshotArchive, SnapshotError
from ..state import StateStore, StateValueError
from .git import WorkspaceError
from .handoff import GitControlRefRepository, WorkflowHandoffControl
from .index import GitIndexRepository, IndexWorkspacePreparer, ensure_pages_root
from .layout import WorkspaceLayout
from .payload import import_workflow_payload
from .publication import UpdateWorkspacePublisher, published_run_status
from .settings import WorkspaceSettings
from .source import GitSourceRepository, clone_repository

MessageSink = Callable[[str], None]
_MINIMUM_SNAPSHOT_BYTES = 100
_MINIMUM_MAIN_SNAPSHOT_BYTES = 100_000
_GH_AUTH_TIMEOUT_SECONDS = 10


def _discard_message(_message: str) -> None:
    return


@dataclass(frozen=True)
class UpdateWorkflowRequest:  # pylint: disable=too-many-instance-attributes
    """Inputs to one repository update workflow."""

    root: Path = Path()
    invocation_directory: Path = Path()
    payload_directory: Path | None = None
    duration: int | None = None
    mode: int | None = None
    owner_request_limit: int = 100
    clone_url: str | None = None
    run_date: date | None = None


@dataclass(frozen=True)
class UpdateApplicationRequest:
    """Run inputs passed from the workspace boundary to application composition."""

    duration: int | None
    mode: int | None
    source_published_today: bool
    working_directory: Path
    owner_request_limit: int
    run_date: date | None


ApplicationRun = Callable[
    [
        UpdateApplicationRequest,
        ApplicationContext,
        WorkflowHandoffControl,
        str | None,
    ],
    ExitStatus,
]


@dataclass(frozen=True)
class UpdateWorkflowExecution:
    """Observable and replaceable effects around one workflow update."""

    run_application: ApplicationRun
    progress: MessageSink = _discard_message
    diagnostic: MessageSink = _discard_message
    clock: Callable[[], float] = time.monotonic


@dataclass(frozen=True)
class PreparedUpdateWorkspace:
    """Services and paths prepared for one application run."""

    root: Path
    repository: GitSourceRepository
    layout: WorkspaceLayout
    state_file: Path
    application: ApplicationContext
    handoff: WorkflowHandoffControl
    handoff_baseline: str | None


class UpdateWorkflowService:  # pylint: disable=too-few-public-methods
    """Own the complete update lifecycle outside the application coordinator."""

    def __init__(self, execution: UpdateWorkflowExecution) -> None:
        self.execution = execution

    def run(self, request: UpdateWorkflowRequest) -> ExitStatus:
        """Prepare, run, validate, and publish one repository update."""

        settings = WorkspaceSettings.from_mapping(SettingsSnapshot.from_env())
        return self._run(request, settings)

    def _run(
        self,
        request: UpdateWorkflowRequest,
        settings: WorkspaceSettings,
    ) -> ExitStatus:
        prepared = self._prepare_update(request, settings)
        source_published_today = self._source_published_today(prepared.repository)
        status = self.execution.run_application(
            UpdateApplicationRequest(
                duration=request.duration,
                mode=request.mode,
                source_published_today=source_published_today,
                working_directory=prepared.root / "src",
                owner_request_limit=request.owner_request_limit,
                run_date=request.run_date,
            ),
            prepared.application,
            prepared.handoff,
            prepared.handoff_baseline,
        )

        with prepared.application.stop.finalization_scope():
            archive = prepared.application.snapshots.current_archive()
        if archive is None or archive.path.stat().st_size < _MINIMUM_SNAPSHOT_BYTES:
            raise SnapshotError("prepared database snapshot is missing or undersized")

        publication_status = published_run_status(status)
        if publication_status is not ExitStatus.SUCCESS:
            self.execution.progress(
                f"Skipping Git publication after run status {int(status)}"
            )
            return publication_status
        UpdateWorkspacePublisher(
            prepared.repository,
            progress=self.execution.progress,
        ).publish(
            prepared.layout.index_name,
            prepared.layout.index_dir,
            prepared.state_file,
        )
        return ExitStatus.SUCCESS

    def _prepare_update(
        self,
        request: UpdateWorkflowRequest,
        settings: WorkspaceSettings,
    ) -> PreparedUpdateWorkspace:
        root = self._root_path(request)
        started_at = self.execution.clock()
        repository, settings = self._ensure_repository(root, request, settings)
        self._log_phase("ensure-root-repo", started_at)

        payload = request.payload_directory or request.invocation_directory / ".bkg"
        import_workflow_payload(payload.resolve(), root)

        started_at = self.execution.clock()
        repository.configure_for_updates(settings.identity)
        self._log_phase("configure-source-repository", started_at)

        handoff = WorkflowHandoffControl(
            GitControlRefRepository(
                repository.path,
                environment=settings.resolved_mapping(),
                redacted_values=settings.redacted_values(),
            ),
            settings.handoff,
            progress=self.execution.progress,
            diagnostic=self.execution.diagnostic,
        )
        baseline = handoff.capture_baseline()
        self._make_shared_workspace(root)

        layout = WorkspaceLayout.discover(root, settings.repository.branch)
        started_at = self.execution.clock()
        preparation = IndexWorkspacePreparer(
            repository,
            progress=self.execution.progress,
            clock=self.execution.clock,
        ).prepare(layout.index_name, layout.index_dir)
        ensure_pages_root(layout.index_dir)
        state_file = root / "src" / "env.env"
        self._prepare_state_file(layout.index_dir / ".env", state_file)
        self._log_phase("prepare-index-workspace", started_at)

        started_at_epoch = int(time.time())
        StateStore(state_file).set_many(
            {
                StateKey.SCRIPT_START: started_at_epoch,
                StateKey.TIMEOUT: 0,
            }
        )
        application = ApplicationContext.from_mapping(
            self._application_mapping(
                settings,
                layout,
                state_file,
                preparation.first_run,
            )
        )
        application.ensure_state_file()
        self._restore_initial_snapshot(application, layout)
        return PreparedUpdateWorkspace(
            root,
            repository,
            layout,
            state_file,
            application,
            handoff,
            baseline,
        )

    def _root_path(self, request: UpdateWorkflowRequest) -> Path:
        root = request.root
        if not root.is_absolute():
            root = request.invocation_directory / root
        return root.resolve()

    def _ensure_repository(
        self,
        root: Path,
        request: UpdateWorkflowRequest,
        settings: WorkspaceSettings,
    ) -> tuple[GitSourceRepository, WorkspaceSettings]:
        repository = self._repository(root, settings)
        if repository.is_worktree():
            resolved = self._resolve_credentials(settings, repository.remote_url())
            return self._repository(root, resolved), resolved
        branch = settings.repository.branch
        if not branch:
            raise WorkspaceError("GITHUB_BRANCH is required to clone the source")
        identity = settings.repository
        source = request.clone_url or (
            f"https://github.com/{identity.owner}/{identity.name}.git"
        )
        resolved = self._resolve_credentials(settings, source)
        root.parent.mkdir(parents=True, exist_ok=True)
        return (
            clone_repository(
                source,
                root,
                branch,
                environment=resolved.resolved_mapping(),
                redacted_values=resolved.redacted_values(),
            ),
            resolved,
        )

    @staticmethod
    def _repository(path: Path, settings: WorkspaceSettings) -> GitSourceRepository:
        return GitSourceRepository(
            path,
            environment=settings.resolved_mapping(),
            redacted_values=settings.redacted_values(),
        )

    @staticmethod
    def _resolve_credentials(
        settings: WorkspaceSettings,
        remote_url: str | None = None,
    ) -> WorkspaceSettings:
        if settings.token:
            return settings
        if remote_url:
            token = _remote_url_token(remote_url)
            if token:
                return settings.with_token(token, "remote-url")
        token = _gh_token(settings.resolved_mapping())
        return settings.with_token(token, "gh" if token else "none")

    def _restore_initial_snapshot(
        self,
        application: ApplicationContext,
        layout: WorkspaceLayout,
    ) -> None:
        archive = application.snapshots.current_archive()
        if archive is None and not layout.index_db.is_file():
            started_at = self.execution.clock()
            self._download_initial_snapshot(application)
            self._log_phase("download-initial-db", started_at)
            archive = application.snapshots.current_archive()

        if archive is not None:
            started_at = self.execution.clock()
            try:
                result = application.snapshots.restore_archive_if_needed(archive)
            except (OSError, SnapshotError) as error:
                self._recover_unhealthy_release(application)
                raise SnapshotError(
                    "Failed to restore the latest database snapshot"
                ) from error
            self.execution.progress(result.message)
            self._log_phase("restore-initial-db", started_at)

        self._ensure_database_is_plausible(application, layout, archive)

    def _download_initial_snapshot(self, application: ApplicationContext) -> None:
        try:
            with application.github_client(report=self.execution.progress) as client:
                asset = application.snapshots.release_snapshot_asset(
                    client,
                    owner=application.config.github_owner,
                    repo=application.config.github_repo,
                )
                if asset is None:
                    self.execution.diagnostic(
                        "No supported database snapshot asset found in release"
                    )
                    return
                result = application.snapshots.download_release_snapshot(client, asset)
        except (GitHubError, OSError, SnapshotError) as error:
            self.execution.diagnostic(str(error))
            return
        self.execution.progress(result.message)
        _ensure_database_gitignore(Path(application.config.root) / ".gitignore")

    def _ensure_database_is_plausible(
        self,
        application: ApplicationContext,
        layout: WorkspaceLayout,
        archive: SnapshotArchive | None,
    ) -> None:
        snapshot_size = _snapshot_or_database_size(archive, layout.index_db)
        owner_count = application.database.packages.package_inventory().owners
        index_owner_count = GitIndexRepository(
            layout.index_dir,
            environment=application.settings.source,
            redacted_values=(application.github_settings.token,),
        ).top_level_directory_count()
        if (
            application.config.github_owner == DEFAULT_GITHUB_OWNER
            and owner_count < index_owner_count // 2
            and snapshot_size < _MINIMUM_MAIN_SNAPSHOT_BYTES
        ):
            backup = Path(f"{layout.index_db}.bak")
            if backup.is_file():
                backup.replace(layout.index_db)
            self._recover_unhealthy_release(application)
            raise SnapshotError("Failed to download the latest database")

    def _recover_unhealthy_release(self, application: ApplicationContext) -> None:
        if application.config.github_owner != DEFAULT_GITHUB_OWNER:
            return
        try:
            with GitHubClient(application.github_settings) as client:
                application.snapshots.delete_unhealthy_releases(
                    client,
                    owner=application.config.github_owner,
                    repo=application.config.github_repo,
                    progress=self.execution.progress,
                )
        except (GitHubError, OSError, SnapshotError) as error:
            self.execution.diagnostic(str(error))

    @staticmethod
    def _prepare_state_file(source: Path, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.is_file():
            shutil.copyfile(source, destination)
        else:
            destination.touch(exist_ok=True)

    @staticmethod
    def _application_mapping(
        settings: WorkspaceSettings,
        layout: WorkspaceLayout,
        state_file: Path,
        first_run: bool,
    ) -> SettingsSnapshot:
        root = layout.root
        return settings.resolved_mapping(
            {
                Env.BKG_ROOT: str(root),
                Env.BKG_ENV: str(state_file),
                Env.BKG_OWNERS: str(root / "owners.txt"),
                Env.BKG_OPTOUT: str(root / "optout.txt"),
                Env.GITHUB_BRANCH: layout.github_branch,
                Env.BKG_INDEX: layout.index_name,
                Env.BKG_INDEX_DB: str(layout.index_db),
                Env.BKG_INDEX_SQL: str(layout.index_sql),
                Env.BKG_INDEX_DIR: str(layout.index_dir),
                Env.BKG_IS_FIRST: str(first_run).lower(),
            }
        )

    @staticmethod
    def _make_shared_workspace(root: Path) -> None:
        _make_shared_path(root)
        for path in root.rglob("*"):
            if path.is_symlink():
                continue
            _make_shared_path(path)

    @staticmethod
    def _source_published_today(repository: GitSourceRepository) -> bool:
        epoch = repository.latest_commit_epoch("master")
        if epoch is None:
            return False
        today = datetime.now(UTC).date()
        return datetime.fromtimestamp(epoch, UTC).date() >= today

    def _log_phase(self, phase: str, started_at: float) -> None:
        elapsed = max(0, int(self.execution.clock() - started_at))
        self.execution.progress(f"Update setup phase '{phase}' completed in {elapsed}s")


def _remote_url_token(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.password:
        return unquote(parsed.password)
    if parsed.username and "@" in parsed.netloc:
        return unquote(parsed.username)
    return ""


def _gh_token(environment: Mapping[str, str]) -> str:
    try:
        gh = resolve_executable("gh")
    except FileNotFoundError:
        return ""
    try:
        status = subprocess.run(  # noqa: S603
            (gh, "auth", "status"),
            check=False,
            capture_output=True,
            env=dict(environment),
            shell=False,
            text=True,
            timeout=_GH_AUTH_TIMEOUT_SECONDS,
        )
        if status.returncode != 0:
            return ""
        token = subprocess.run(  # noqa: S603
            (gh, "auth", "token"),
            check=False,
            capture_output=True,
            env=dict(environment),
            shell=False,
            text=True,
            timeout=_GH_AUTH_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return ""
    return token.stdout.strip() if token.returncode == 0 else ""


def _ensure_database_gitignore(path: Path) -> None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []
    if not any("*.db" in line for line in lines):
        lines.append("*.db*")
        content = "\n".join(lines)
        path.write_text(f"{content}\n", encoding="utf-8")


def _make_shared_path(path: Path) -> None:
    mode = path.stat().st_mode
    path.chmod(mode | (0o2777 if path.is_dir() else 0o666))


def _snapshot_or_database_size(
    archive: SnapshotArchive | None,
    database: Path,
) -> int:
    candidate = archive.path if archive is not None else database
    try:
        return candidate.stat().st_size
    except FileNotFoundError:
        return 0


def run_update_workflow(
    request: UpdateWorkflowRequest,
    execution: UpdateWorkflowExecution,
) -> ExitStatus:
    """Run the update service with shell-facing error translation."""

    service = UpdateWorkflowService(execution)
    try:
        return service.run(request)
    except (
        GitHubError,
        GracefulStop,
        OSError,
        SnapshotError,
        StateValueError,
        ValueError,
        WorkspaceError,
    ) as error:
        execution.diagnostic(str(error))
        return ExitStatus.NON_FATAL
