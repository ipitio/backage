"""Owner and repository aggregate publication from committed database state."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from .database import DatabaseRepository
from .publication import PublicationLimits, publish_json_file
from .rendering import (
    AggregateSettings,
    DatabaseAggregateOptions,
    render_database_aggregate,
)

StopCheck = Callable[[], None]


@dataclass(frozen=True)
class OwnerPublicationRequest:
    """Filesystem and database identity for one owner publication."""

    owner_id: str
    owner: str
    index_directory: Path


@dataclass(frozen=True)
class OwnerPublicationResult:
    """Published package and repository counts for one owner."""

    package_count: int
    repositories: tuple[str, ...]


class OwnerPublicationService:  # pylint: disable=too-few-public-methods
    """Publish one owner's aggregate endpoints as atomic JSON/XML pairs."""

    def __init__(
        self,
        repository: DatabaseRepository,
        aggregate_settings: AggregateSettings,
        publication_limits: PublicationLimits,
        check_stop: StopCheck,
    ) -> None:
        self.repository = repository
        self.aggregate_settings = aggregate_settings
        self.publication_limits = publication_limits
        self.check_stop = check_stop

    def publish(self, request: OwnerPublicationRequest) -> OwnerPublicationResult:
        """Render all current aggregate endpoints for one owner."""

        owner_directory = request.index_directory / request.owner
        owner_directory.mkdir(parents=True, exist_ok=True)
        package_count = self._publish_aggregate(
            request.owner_id,
            owner_directory / ".json",
            repo=None,
            size_hint_directory=owner_directory,
        )
        if package_count == 0:
            _remove_endpoint(owner_directory / ".json")
            with suppress(OSError):
                owner_directory.rmdir()
            return OwnerPublicationResult(0, ())

        published_repositories: list[str] = []
        for repo in self.repository.repository_names(request.owner_id):
            self.check_stop()
            repo_directory = owner_directory / repo
            repo_directory.mkdir(parents=True, exist_ok=True)
            count = self._publish_aggregate(
                request.owner_id,
                repo_directory / ".json",
                repo=repo,
                size_hint_directory=repo_directory,
            )
            if count > 0:
                published_repositories.append(repo)
            else:
                _remove_endpoint(repo_directory / ".json")

        return OwnerPublicationResult(package_count, tuple(published_repositories))

    def _publish_aggregate(
        self,
        owner_id: str,
        destination: Path,
        *,
        repo: str | None,
        size_hint_directory: Path,
    ) -> int:
        descriptor, temporary_name = tempfile.mkstemp(
            dir=destination.parent,
            prefix=f".{destination.name}.render.",
        )
        os.close(descriptor)
        source = Path(temporary_name)
        try:
            count = render_database_aggregate(
                self.repository,
                owner_id,
                source,
                DatabaseAggregateOptions(
                    repo=repo,
                    size_hint_directory=size_hint_directory,
                    settings=self.aggregate_settings,
                ),
                self.check_stop,
            )
            if count > 0:
                self.check_stop()
                publish_json_file(
                    source,
                    self.check_stop,
                    self.publication_limits,
                    destination,
                )
            return count
        finally:
            source.unlink(missing_ok=True)


def _remove_endpoint(json_path: Path) -> None:
    json_path.unlink(missing_ok=True)
    xml_path = (
        json_path.with_name(".xml")
        if json_path.name == ".json"
        else json_path.with_suffix(".xml")
    )
    xml_path.unlink(missing_ok=True)
