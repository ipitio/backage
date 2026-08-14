"""Typed package-work planning for top-level application orchestration."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

from .database import PackageWorkItem, PackageWorkPlan
from .files import atomic_text_output
from .runtime_names import RunFile


class PackageWorkPlanRepository(Protocol):  # pylint: disable=too-few-public-methods
    """Database read needed to prepare one package-work plan."""

    def package_work_plan(
        self,
        since: str,
        batch_marker: str = "",
    ) -> PackageWorkPlan:
        """Return current package work and owner ordering."""

        raise NotImplementedError


@dataclass(frozen=True)
class PackageWorkPlanSummary:
    """Package counts reported to the run coordinator."""

    total: int
    completed: int
    pending: int


class PackageWorkPlanService:  # pylint: disable=too-few-public-methods
    """Build typed package work and publish live run intermediates."""

    def __init__(self, repository: PackageWorkPlanRepository) -> None:
        self.repository = repository

    def prepare(
        self,
        since: str,
        directory: Path,
        *,
        batch_marker: str = "",
        reset: bool = False,
    ) -> PackageWorkPlanSummary:
        """Write the bounded planning inputs consumed by this run."""

        plan = self.repository.package_work_plan(since, batch_marker)
        if reset:
            plan = replace(plan, completed=(), pending=plan.packages)
        directory.mkdir(parents=True, exist_ok=True)
        _write_items(directory / RunFile.PACKAGES_ALL, plan.packages)
        _write_lines(directory / RunFile.ALL_OWNERS_IN_DB, plan.owners)
        _write_lines(
            directory / RunFile.OWNERS_PARTIALLY_UPDATED,
            plan.partially_updated_owners,
        )
        _write_lines(directory / RunFile.OWNERS_STALE, plan.stale_owners)
        _write_lines(
            directory / RunFile.OWNERS_SCANNED_WITHOUT_PACKAGES,
            plan.scanned_without_packages,
        )
        return PackageWorkPlanSummary(
            len(plan.packages),
            len(plan.completed),
            len(plan.pending),
        )


def _write_items(path: Path, items: tuple[PackageWorkItem, ...]) -> None:
    _write_lines(
        path,
        tuple(
            "|".join((item.owner_id, item.owner, item.repo, item.package, item.date))
            for item in items
        ),
    )


def _write_lines(path: Path, lines: tuple[str, ...]) -> None:
    with atomic_text_output(path) as output:
        if lines:
            output.write("\n".join(lines))
            output.write("\n")
