"""Tests for typed package-work planning and live run intermediates."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from bkg_py.database import PackageWorkItem, PackageWorkPlan
from bkg_py.run.planning import PackageWorkPlanService
from bkg_py.runtime_names import RunFile


@dataclass
class _Repository:
    plan: PackageWorkPlan
    requested_since: list[str] = field(default_factory=list[str])

    def package_work_plan(
        self,
        since: str,
        batch_marker: str = "",
    ) -> PackageWorkPlan:
        """Return the configured plan and record its batch date."""

        del batch_marker
        self.requested_since.append(since)
        return self.plan


def test_package_work_plan_writes_consumed_intermediate_formats(
    tmp_path: Path,
) -> None:
    """The planner publishes only inputs consumed by the current runtime."""

    first = PackageWorkItem("1", "Alpha", "repo-a", "pkg-a", "2026-06-28")
    second = PackageWorkItem("2", "Beta", "repo-b", "pkg-b", "2026-06-29")
    repository = _Repository(
        PackageWorkPlan(
            packages=(first, second),
            completed=(second,),
            pending=(first,),
            owners=("Alpha", "Beta", "Empty"),
            scanned_without_packages=("Empty",),
        )
    )
    output = tmp_path / "plan"

    summary = PackageWorkPlanService(repository).prepare("2026-06-29", output)

    assert summary.total == 2
    assert summary.completed == 1
    assert summary.pending == 1
    assert repository.requested_since == ["2026-06-29"]
    assert (output / RunFile.PACKAGES_ALL).read_text(encoding="utf-8") == (
        "1|Alpha|repo-a|pkg-a|2026-06-28\n2|Beta|repo-b|pkg-b|2026-06-29\n"
    )
    assert (output / RunFile.ALL_OWNERS_IN_DB).read_text(encoding="utf-8") == (
        "Alpha\nBeta\nEmpty\n"
    )
    assert (output / RunFile.OWNERS_PARTIALLY_UPDATED).read_text(encoding="utf-8") == ""
    assert (output / RunFile.OWNERS_STALE).read_text(encoding="utf-8") == "Alpha\n"
    assert (output / RunFile.OWNERS_SCANNED_WITHOUT_PACKAGES).read_text(
        encoding="utf-8"
    ) == "Empty\n"
    for legacy in (
        RunFile.LEGACY_PACKAGES_ALREADY_UPDATED,
        RunFile.LEGACY_PACKAGES_TO_UPDATE,
        RunFile.LEGACY_ALL_OWNERS_TO_UPDATE,
        RunFile.LEGACY_OWNERS_UPDATED,
        RunFile.LEGACY_OWNERS_DEFERRED,
    ):
        assert not (output / legacy).exists()


def test_reset_plan_marks_every_package_pending(tmp_path: Path) -> None:
    """A same-day batch reset does not inherit earlier completed work."""

    package = PackageWorkItem("1", "Alpha", "repo", "pkg", "2026-07-01")
    repository = _Repository(
        PackageWorkPlan(
            packages=(package,),
            completed=(package,),
            pending=(),
            owners=("Alpha",),
            scanned_without_packages=(),
        )
    )

    summary = PackageWorkPlanService(repository).prepare(
        "2026-07-01",
        tmp_path,
        reset=True,
    )

    assert summary.completed == 0
    assert summary.pending == 1
    assert (tmp_path / RunFile.PACKAGES_ALL).read_text(encoding="utf-8") == (
        "1|Alpha|repo|pkg|2026-07-01\n"
    )
    assert (tmp_path / RunFile.OWNERS_STALE).read_text(encoding="utf-8") == "Alpha\n"


def test_package_plan_classifies_partial_owners_in_pending_order() -> None:
    """Owner queue inputs preserve pending order and split partial from stale."""

    alpha_done = PackageWorkItem("1", "Alpha", "one", "done", "2026-07-01")
    alpha_pending = PackageWorkItem("1", "Alpha", "one", "todo", "2026-06-30")
    beta_pending = PackageWorkItem("2", "Beta", "two", "todo", "2026-06-30")
    plan = PackageWorkPlan(
        packages=(alpha_done, alpha_pending, beta_pending),
        completed=(alpha_done,),
        pending=(beta_pending, alpha_pending),
        owners=("Alpha", "Beta"),
        scanned_without_packages=(),
    )

    assert plan.updated_owners == ("Alpha",)
    assert plan.pending_owners == ("Beta", "Alpha")
    assert plan.partially_updated_owners == ("Alpha",)
    assert plan.stale_owners == ("Beta",)
