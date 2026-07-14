"""Publish top-level run summaries after database updates finish."""

from __future__ import annotations

import json
import re
import tempfile
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Protocol

from .database import PackageInventory
from .files import atomic_binary_output, atomic_text_output
from .publication import publish_json_file
from .state import StateStore

StopCheck = Callable[[], None]
_NUMBER_SUFFIXES = ("", "k", "M", "B", "T", "P", "E", "Z", "Y")
_SIDECAR_MARKERS = (".json.tmp", ".json.abs", ".json.rel")
_TRANSIENT_STATE_PREFIXES = (
    "BKG_VERSIONS_",
    "BKG_PACKAGES_",
    "BKG_OWNERS_",
    "BKG_OWNER_SCAN_",
)
_NUMBERED_PAGE_KEY = re.compile(r"BKG_PAGE_[0-9].*")
_INTERMEDIATE_FILES = (
    "packages_already_updated",
    "packages_all",
    "packages_to_update",
)


class PackageInventoryRepository(Protocol):  # pylint: disable=too-few-public-methods
    """Database read needed for final run publication."""

    def package_inventory(self) -> PackageInventory:
        """Return current package, owner, and repository counts."""

        raise NotImplementedError


@dataclass(frozen=True)
class RunPublicationPaths:
    """Filesystem locations used by final run publication."""

    root: Path
    index_directory: Path
    working_directory: Path


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
    rotated: bool


class RunPublicationService:  # pylint: disable=too-few-public-methods
    """Hydrate source and index summaries from committed database state."""

    def __init__(
        self,
        repository: PackageInventoryRepository,
        state: StateStore,
        check_stop: StopCheck,
    ) -> None:
        self.repository = repository
        self.state = state
        self.check_stop = check_stop

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

        _prune_transient_state(self.state)
        for name in _INTERMEDIATE_FILES:
            with suppress(FileNotFoundError):
                (request.paths.working_directory / name).unlink()
        return inventory


@dataclass(frozen=True)
class _PublicationSources:
    changelog: str
    readme: str
    index_html: str
    logo: bytes
    favicon: bytes
    javascript: bytes


def _validate_request(request: RunPublicationRequest) -> None:
    try:
        parsed = date.fromisoformat(request.today)
    except ValueError as error:
        raise ValueError(f"invalid UTC run date: {request.today}") from error
    if parsed.isoformat() != request.today:
        raise ValueError(f"invalid UTC run date: {request.today}")
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
    if request.rotated:
        result += (
            "P.S. The database was rotated, but you can find all previous data "
            "under the [latest release](https://github.com/"
            f"{request.identity.github_owner}/{request.identity.github_repo}/"
            "releases/latest).\n"
        )
    return result


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
    numbered_page_keys = tuple(
        key for key in state.snapshot() if _NUMBERED_PAGE_KEY.fullmatch(key)
    )
    state.delete_matching(
        keys=numbered_page_keys,
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
