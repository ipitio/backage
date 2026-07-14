"""Tests for the pooled authenticated discovery phase."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from bkg_py.discovery_operations import (
    DiscoveryPhaseExecution,
    DiscoveryPhaseIdentity,
    DiscoveryPhasePaths,
    DiscoveryPhaseRequest,
    DiscoveryPhaseService,
    DiscoveryPhaseServices,
)
from bkg_py.owners.pages import OwnerPageAdmissionResult


@dataclass
class _Traversal:
    membership_values: tuple[str, ...] = ()
    explored: list[str] = field(default_factory=list[str])
    expanded: list[str] = field(default_factory=list[str])
    memberships: list[str] = field(default_factory=list[str])

    def explore(self, node: str, edge: str = "") -> tuple[str, ...]:
        """Return deterministic owner and repository connection fixtures."""

        assert not edge
        self.explored.append(node)
        if "/" in node:
            return ("99/Gamma", "beta")
        return ("beta", "alpha", "solutions", "alpha")

    def organization_logins(self, owner_ref: str) -> tuple[str, ...]:
        """Return overlapping organization memberships for connection owners."""

        self.expanded.append(owner_ref)
        return {
            "beta": ("OrgB", "OrgA"),
            "alpha": ("OrgA",),
        }.get(owner_ref, ())

    def membership(self, owner_ref: str) -> tuple[str, ...]:
        """Return configured membership values for a fork deployment."""

        self.memberships.append(owner_ref)
        return self.membership_values


@dataclass
class _Admission:
    calls: list[tuple[int, int]] = field(default_factory=list[tuple[int, int]])

    def __call__(self, page: int, per_page: int) -> OwnerPageAdmissionResult:
        self.calls.append((page, per_page))
        if page == 1:
            return OwnerPageAdmissionResult(1, 1, True, ("RequestedOne",))
        return OwnerPageAdmissionResult(2, 2, False, ("RequestedTwo",))


def _clock(*values: float):
    iterator = iter(values)
    return lambda: next(iterator)


def test_primary_discovery_pools_traversal_expansion_and_owner_pages(
    tmp_path: Path,
) -> None:
    """The primary path publishes connections and completes its daily gate."""

    connections = tmp_path / "connections"
    owners = tmp_path / "owners.txt"
    optout = tmp_path / "optout.txt"
    owners.write_text("".join(f"owner-{index}\n" for index in range(150)))
    optout.write_text("", encoding="utf-8")
    traversal = _Traversal()
    admission = _Admission()
    completed: list[str] = []
    messages: list[str] = []
    service = DiscoveryPhaseService(
        DiscoveryPhaseServices(traversal, admission, completed.append),
        DiscoveryPhaseExecution(
            lambda: None,
            messages.append,
            _clock(0, 2, 2, 5, 5, 9),
        ),
    )

    result = service.run(
        DiscoveryPhaseRequest(
            DiscoveryPhasePaths(connections, owners, optout),
            DiscoveryPhaseIdentity("ipitio", "backage", True),
            "2026-07-03",
            False,
            False,
            5,
        )
    )

    assert result.connections == 5
    assert result.owner_pages == 2
    assert result.admitted_owners == 3
    assert traversal.explored == ["ipitio", "ipitio/backage"]
    assert traversal.expanded == ["beta", "alpha", "99/Gamma"]
    assert connections.read_text(encoding="utf-8") == (
        "beta\nalpha\n99/Gamma\nOrgB\nOrgA\n"
    )
    assert completed == ["2026-07-03"]
    assert admission.calls == [(1, 1), (2, 1)]
    assert "Requested RequestedOne" in messages
    assert "Requested RequestedTwo" in messages
    assert "Startup phase 'discover-connections' completed in 2s" in messages
    assert "Startup phase 'expand-connection-orgs' completed in 3s" in messages
    assert "Startup phase 'page-owner-discovery' completed in 4s" in messages


def test_membership_discovery_resets_first_run_inputs(tmp_path: Path) -> None:
    """A fork membership pass writes connections and resets first-run sources."""

    connections = tmp_path / "connections"
    owners = tmp_path / "owners.txt"
    optout = tmp_path / "optout.txt"
    owners.write_text("old-owner\n", encoding="utf-8")
    optout.write_text("old-optout\n", encoding="utf-8")
    traversal = _Traversal(("Member", "Member", '"Org"', "premium-support"))
    messages: list[str] = []
    service = DiscoveryPhaseService(
        DiscoveryPhaseServices(
            traversal,
            lambda _page, _per_page: OwnerPageAdmissionResult(0, 0, False),
            lambda _today: None,
        ),
        DiscoveryPhaseExecution(lambda: None, messages.append, _clock(10, 12)),
    )

    result = service.run(
        DiscoveryPhaseRequest(
            DiscoveryPhasePaths(connections, owners, optout),
            DiscoveryPhaseIdentity("fork-owner", "backage", False),
            "2026-07-03",
            False,
            True,
            1,
        )
    )

    assert result.connections == 2
    assert traversal.memberships == ["fork-owner"]
    assert connections.read_text(encoding="utf-8") == "Member\nOrg\n"
    assert owners.read_text(encoding="utf-8") == ""
    assert optout.read_text(encoding="utf-8") == ""
    assert messages == ["Startup phase 'discover-membership' completed in 2s"]
