"""Typed package-work planning for top-level application orchestration."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

from .database_models import PackageWorkItem, PackageWorkPlan
from .files import atomic_text_output


class PackageWorkPlanRepository(Protocol):  # pylint: disable=too-few-public-methods
    """Database read needed to prepare one package-work plan."""

    def package_work_plan(self, since: str) -> PackageWorkPlan:
        """Return current package work and owner ordering."""

        raise NotImplementedError


@dataclass(frozen=True)
class PackageWorkPlanSummary:
    """Counts emitted to the compatibility launcher."""

    total: int
    completed: int
    pending: int


class PackageWorkPlanService:  # pylint: disable=too-few-public-methods
    """Build typed package work and publish legacy intermediate files."""

    def __init__(self, repository: PackageWorkPlanRepository) -> None:
        self.repository = repository

    def prepare(
        self,
        since: str,
        directory: Path,
        *,
        reset: bool = False,
    ) -> PackageWorkPlanSummary:
        """Write one package plan while preserving current file formats."""

        plan = self.repository.package_work_plan(since)
        if reset:
            plan = replace(plan, completed=(), pending=plan.packages)
        directory.mkdir(parents=True, exist_ok=True)
        _write_items(directory / "packages_all", plan.packages)
        _write_items(directory / "packages_already_updated", plan.completed)
        _write_items(directory / "packages_to_update", plan.pending)
        _write_lines(directory / "all_owners_in_db", plan.owners)
        _write_lines(directory / "owners_updated", plan.updated_owners)
        _write_lines(directory / "all_owners_tu", plan.pending_owners)
        _write_lines(
            directory / "owners_partially_updated",
            plan.partially_updated_owners,
        )
        _write_lines(directory / "owners_stale", plan.stale_owners)
        _write_lines(
            directory / "owners_scanned_without_packages",
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
