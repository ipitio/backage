"""Tests for typed package-work planning and compatibility output files."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from bkg_py.database_models import PackageWorkItem, PackageWorkPlan
from bkg_py.run_planning import PackageWorkPlanService


@dataclass
class _Repository:
    plan: PackageWorkPlan
    requested_since: list[str] = field(default_factory=list[str])

    def package_work_plan(self, since: str) -> PackageWorkPlan:
        """Return the configured plan and record its batch date."""

        self.requested_since.append(since)
        return self.plan


def test_package_work_plan_writes_existing_intermediate_formats(
    tmp_path: Path,
) -> None:
    """The Python planner remains compatible with downstream shell readers."""

    first = PackageWorkItem("1", "Alpha", "repo-a", "pkg-a", "2026-06-28")
    second = PackageWorkItem("2", "Beta", "repo-b", "pkg-b", "2026-06-29")
    repository = _Repository(
        PackageWorkPlan(
            packages=(first, second),
            completed=(second,),
            pending=(first,),
            owners=("Alpha", "Beta", "Empty"),
        )
    )
    output = tmp_path / "plan"

    summary = PackageWorkPlanService(repository).prepare("2026-06-29", output)

    assert summary.total == 2
    assert summary.completed == 1
    assert summary.pending == 1
    assert repository.requested_since == ["2026-06-29"]
    assert (output / "packages_all").read_text(encoding="utf-8") == (
        "1|Alpha|repo-a|pkg-a|2026-06-28\n2|Beta|repo-b|pkg-b|2026-06-29\n"
    )
    assert (output / "packages_already_updated").read_text(
        encoding="utf-8"
    ) == "2|Beta|repo-b|pkg-b|2026-06-29\n"
    assert (output / "packages_to_update").read_text(
        encoding="utf-8"
    ) == "1|Alpha|repo-a|pkg-a|2026-06-28\n"
    assert (output / "all_owners_in_db").read_text(encoding="utf-8") == (
        "Alpha\nBeta\nEmpty\n"
    )
