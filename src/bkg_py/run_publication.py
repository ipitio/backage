"""Publish top-level run summaries after database updates finish."""

from __future__ import annotations

import json
import re
import tempfile
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Protocol

from .dashboard import DASHBOARD_SCHEMA_VERSION, publish_dashboard
from .database import (
    DashboardProjection,
    DatabaseError,
    DatabaseRotationEvent,
    PackageInventory,
)
from .files import atomic_binary_output, atomic_text_output
from .publication import publish_json_file
from .release import release_tag as release_tag_for_date
from .site_shell import (
    GitHubRepositoryIdentity,
    SiteShellError,
    default_site_shell_directory,
    publish_site_shell,
)
from .state import StateStore

StopCheck = Callable[[], None]
MessageSink = Callable[[str], None]
_NUMBER_SUFFIXES = ("", "k", "M", "B", "T", "P", "E", "Z", "Y")
_SIDECAR_MARKERS = (".json.tmp", ".json.abs", ".json.rel")
_TRANSIENT_STATE_PREFIXES = (
    "BKG_VERSIONS_",
    "BKG_PACKAGES_",
    "BKG_OWNER_SCAN_",
)
_TRANSIENT_OWNER_PREFIX = "BKG_OWNERS_"
_NUMBERED_PAGE_KEY = re.compile(r"BKG_PAGE_[0-9].*")
_OBSOLETE_STATE_KEYS = ("BKG_PAGE_ALL",)
_INTERMEDIATE_FILES = (
    "packages_already_updated",
    "packages_all",
    "packages_to_update",
)


class RunPublicationRepository(Protocol):  # pylint: disable=too-few-public-methods
    """Database read needed for final run publication."""

    def package_inventory(self) -> PackageInventory:
        """Return current package, owner, and repository counts."""

        raise NotImplementedError

    def dashboard_projection(self, today: str) -> DashboardProjection:
        """Return bounded analytics from the current catalog snapshot."""

        raise NotImplementedError


@dataclass(frozen=True)
class RunPublicationPaths:
    """Filesystem locations used by final run publication."""

    root: Path
    index_directory: Path
    working_directory: Path
    site_shell_directory: Path = field(default_factory=default_site_shell_directory)


@dataclass(frozen=True)
class RunPublicationIdentity:
    """Repository substitutions used by final run publication."""

    github_owner: str
    github_repo: str
    github_branch: str


@dataclass(frozen=True)
class RunPublicationRequest:
    """Inputs for one final run publication."""

    paths: RunPublicationPaths
    identity: RunPublicationIdentity
    today: str
    rotation_events: tuple[DatabaseRotationEvent, ...] = ()


class RunPublicationService:  # pylint: disable=too-few-public-methods
    """Hydrate source and index summaries from committed database state."""

    def __init__(
        self,
        repository: RunPublicationRepository,
        state: StateStore,
        check_stop: StopCheck,
        progress: MessageSink | None = None,
    ) -> None:
        self.repository = repository
        self.state = state
        self.check_stop = check_stop
        self.progress = progress or _ignore_message

    def publish(self, request: RunPublicationRequest) -> PackageInventory:
        """Atomically replace each generated summary and remove transient state."""

        _validate_request(request)
        sources = _read_sources(request.paths.root)
        inventory = self.repository.package_inventory()
        self.check_stop()

        changelog = _render_changelog(
            sources.changelog,
            request,
            inventory,
        )
        readme = _render_readme(sources.readme, request, inventory)
        index_readme = _index_readme(readme)
        index_html = sources.index_html.replace(
            "GITHUB_REPO", request.identity.github_repo
        )

        index_directory = request.paths.index_directory
        index_directory.mkdir(parents=True, exist_ok=True)
        _cleanup_sidecars(index_directory, self.check_stop)
        _write_text(request.paths.root / "CHANGELOG.md", changelog)
        _write_text(request.paths.root / "README.md", readme)
        _write_text(index_directory / "README.md", index_readme)
        _write_bytes(index_directory / "logo-b.webp", sources.logo)
        _write_bytes(index_directory / "favicon.ico", sources.favicon)
        _write_text(index_directory / "index.html", index_html)
        _write_bytes(index_directory / "fxp.min.js", sources.javascript)
        _publish_index_summary(
            index_directory,
            request.today,
            inventory,
            self.check_stop,
        )
        self._publish_dashboard(index_directory, request.today, inventory)
        self._publish_site_shell(
            index_directory,
            request.paths.site_shell_directory,
            request.identity,
        )

        _prune_transient_state(self.state)
        for name in _INTERMEDIATE_FILES:
            with suppress(FileNotFoundError):
                (request.paths.working_directory / name).unlink()
        return inventory

    def _publish_dashboard(
        self,
        index_directory: Path,
        today: str,
        inventory: PackageInventory,
    ) -> None:
        query_started = time.monotonic()
        try:
            projection = self.repository.dashboard_projection(today)
        except DatabaseError as error:
            self.progress(
                f"Dashboard projection unavailable; keeping previous artifacts: {error}"
            )
            return
        query_seconds = max(0.0, time.monotonic() - query_started)
        if projection.inventory != inventory:
            self.progress(
                "Dashboard projection inventory changed during finalization; "
                "keeping previous artifacts"
            )
            return

        write_started = time.monotonic()
        try:
            result = publish_dashboard(
                projection,
                index_directory,
                today,
                self.check_stop,
            )
        except OSError as error:
            self.progress(
                "Dashboard publication unavailable; keeping previous artifacts: "
                f"{error}"
            )
            return
        write_seconds = max(0.0, time.monotonic() - write_started)
        self.progress(
            "Dashboard publication telemetry: "
            + json.dumps(
                {
                    "dashboard_bytes": result.dashboard_bytes,
                    "history_bytes": result.history_bytes,
                    "history_reset": result.history_reset,
                    "history_samples": result.history_samples,
                    "query_seconds": round(query_seconds, 3),
                    "write_seconds": round(write_seconds, 3),
                },
                separators=(",", ":"),
                sort_keys=True,
            )
        )

    def _publish_site_shell(
        self,
        index_directory: Path,
        site_shell_directory: Path,
        identity: RunPublicationIdentity,
    ) -> None:
        try:
            result = publish_site_shell(
                site_shell_directory,
                index_directory,
                dashboard_schema_version=DASHBOARD_SCHEMA_VERSION,
                repository=GitHubRepositoryIdentity(
                    identity.github_owner,
                    identity.github_repo,
                ),
                check_stop=self.check_stop,
            )
        except (OSError, SiteShellError) as error:
            self.progress(
                "Site shell publication unavailable; retaining current usable "
                f"shell state: {error}"
            )
            return
        self.progress(
            "Site shell publication telemetry: "
            + json.dumps(
                {
                    "bytes": result.bytes,
                    "entrypoint": result.entrypoint,
                    "files": result.files,
                    "removed_files": result.removed_files,
                    "site_shell_version": result.site_shell_version,
                },
                separators=(",", ":"),
                sort_keys=True,
            )
        )


@dataclass(frozen=True)
class _PublicationSources:
    changelog: str
    readme: str
    index_html: str
    logo: bytes
    favicon: bytes
    javascript: bytes


def _ignore_message(_message: str) -> None:
    return


def _validate_request(request: RunPublicationRequest) -> None:
    try:
        parsed = date.fromisoformat(request.today)
    except ValueError as error:
        raise ValueError(f"invalid UTC run date: {request.today}") from error
    if parsed.isoformat() != request.today:
        raise ValueError(f"invalid UTC run date: {request.today}")
    expected_release_tag = release_tag_for_date(parsed)
    if any(
        event.release_tag != expected_release_tag for event in request.rotation_events
    ):
        raise ValueError(
            f"rotation events do not belong to release {expected_release_tag}"
        )
    for name, value in (
        ("GitHub owner", request.identity.github_owner),
        ("GitHub repository", request.identity.github_repo),
        ("GitHub branch", request.identity.github_branch),
    ):
        if not value:
            raise ValueError(f"{name} is required for run publication")


def _read_sources(root: Path) -> _PublicationSources:
    templates = root / "src" / "templates"
    images = root / "src" / "img"
    return _PublicationSources(
        changelog=(templates / ".CHANGELOG.md").read_text(encoding="utf-8"),
        readme=(templates / ".README.md").read_text(encoding="utf-8"),
        index_html=(templates / ".index.html").read_text(encoding="utf-8"),
        logo=(images / "logo-b.webp").read_bytes(),
        favicon=(images / "logo.ico").read_bytes(),
        javascript=(templates / "fxp.min.js").read_bytes(),
    )


def _render_changelog(
    template: str,
    request: RunPublicationRequest,
    inventory: PackageInventory,
) -> str:
    result = _replace_summary_values(template, request.today, inventory)
    if request.rotation_events:
        result += _render_rotation_events(request)
    return result


def _render_rotation_events(request: RunPublicationRequest) -> str:
    events = request.rotation_events
    release_tag = events[0].release_tag
    release_url = (
        "https://github.com/"
        f"{request.identity.github_owner}/{request.identity.github_repo}/"
        f"releases/tag/{release_tag}"
    )
    lines = [
        "P.S. The database was rotated during this release. Earlier database "
        f"snapshots are retained with the [{release_tag} release]({release_url}).",
        "",
        "Rotation archives:",
    ]
    lines.extend(
        f"- `{event.archive_name}`: rotated {event.rotated_at}; "
        f"source {event.source_bytes} bytes; compressed "
        f"{event.compressed_bytes} bytes; live history retained from "
        f"{event.retained_since}."
        for event in events
    )
    return "\n".join((*lines, ""))


def _render_readme(
    template: str,
    request: RunPublicationRequest,
    inventory: PackageInventory,
) -> str:
    return (
        _replace_summary_values(template, request.today, inventory)
        .replace("<GITHUB_OWNER>", request.identity.github_owner)
        .replace("<GITHUB_REPO>", request.identity.github_repo)
        .replace("<GITHUB_BRANCH>", request.identity.github_branch)
    )


def _replace_summary_values(
    template: str,
    today: str,
    inventory: PackageInventory,
) -> str:
    return (
        template.replace("[DATE]", today)
        .replace("[OWNERS]", str(inventory.owners))
        .replace("[REPOS]", str(inventory.repositories))
        .replace("[PACKAGES]", str(inventory.packages))
    )


def _index_readme(readme: str) -> str:
    return (
        readme.replace("src/img/logo-b.webp", "logo-b.webp")
        .replace("```py", "```prolog")
        .replace("```js", "```jboss-cli")
    )


def _publish_index_summary(
    index_directory: Path,
    today: str,
    inventory: PackageInventory,
    check_stop: StopCheck,
) -> None:
    value = {
        "owners": _compact_number(inventory.owners),
        "repos": _compact_number(inventory.repositories),
        "packages": _compact_number(inventory.packages),
        "raw_owners": inventory.owners,
        "raw_repos": inventory.repositories,
        "raw_packages": inventory.packages,
        "date": today,
    }
    with tempfile.TemporaryDirectory(
        dir=index_directory,
        prefix=".run-summary-",
    ) as directory:
        source = Path(directory) / "summary.json"
        source.write_text(
            f"{json.dumps(value, separators=(',', ':'))}\n",
            encoding="utf-8",
        )
        publish_json_file(
            source,
            check_stop,
            destination=index_directory / ".json",
        )


def _compact_number(value: int) -> str:
    scaled = Decimal(value)
    suffix_index = 0
    while scaled > Decimal("999.9"):
        scaled /= 1000
        suffix_index += 1
    scaled = scaled.quantize(Decimal("0.1"), rounding=ROUND_DOWN)
    number = format(scaled, "f").rstrip("0").rstrip(".")
    suffix = (
        _NUMBER_SUFFIXES[suffix_index] if suffix_index < len(_NUMBER_SUFFIXES) else ""
    )
    return f"{number}{suffix}"


def _cleanup_sidecars(index_directory: Path, check_stop: StopCheck) -> None:
    for index, path in enumerate(index_directory.rglob("*")):
        if index % 1024 == 0:
            check_stop()
        if not path.is_file() or not _is_sidecar(path.name):
            continue
        with suppress(OSError):
            path.unlink()


def _prune_transient_state(state: StateStore) -> None:
    transient_keys = tuple(
        key
        for key in state.snapshot()
        if _NUMBERED_PAGE_KEY.fullmatch(key) or key.startswith(_TRANSIENT_OWNER_PREFIX)
    )
    state.delete_matching(
        keys=(*_OBSOLETE_STATE_KEYS, *transient_keys),
        prefixes=_TRANSIENT_STATE_PREFIXES,
    )


def _is_sidecar(name: str) -> bool:
    return any(
        name.endswith(marker) or f"{marker}." in name for marker in _SIDECAR_MARKERS
    )


def _write_text(destination: Path, content: str) -> None:
    with atomic_text_output(destination) as output:
        output.write(content)


def _write_bytes(destination: Path, content: bytes) -> None:
    with atomic_binary_output(destination) as output:
        output.write(content)
