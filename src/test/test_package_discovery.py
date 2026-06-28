"""Tests for owner package listing discovery."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

import bkg_py.package_commands
from bkg_py.application import ApplicationContext
from bkg_py.cli import main
from bkg_py.database import DatabaseRepository, DatabaseSettings
from bkg_py.database_models import OwnerScanPackage
from bkg_py.github import GitHubNotFoundError
from bkg_py.package_discovery import (
    PackageListingPage,
    PackageListingRequest,
    PackageListingService,
    parse_package_listing_html,
)
from bkg_py.result import ExitStatus
from bkg_py.state import StateStore

from .github_client_fake import FakeGitHubClient


def test_listing_parser_associates_repositories_and_deduplicates_packages() -> None:
    """Packages use their following repository without crossing card boundaries."""

    request = PackageListingRequest("orgs", "Example", 1, 0)
    html = """
        <a href="/orgs/Example/packages/container/package/alpha">alpha</a>
        <a href="/orgs/Example/packages/container/package/alpha">alpha icon</a>
        <a href="/Example/AlphaRepo">repository</a>
        <a href="/orgs/Example/packages/container/package/alpha">alpha</a>
        <a href="/Example/AlphaRepo">repository</a>
        <a href="/orgs/Example/packages/npm/package/tools%2Fworker">worker</a>
        <a href="/orgs/Example/packages/npm/package/beta">beta</a>
        <a href="/Example/BetaRepo">repository</a>
    """

    page = parse_package_listing_html(html, request)

    assert page.packages == (
        OwnerScanPackage("orgs", "container", "AlphaRepo", "alpha"),
        OwnerScanPackage("orgs", "npm", "BetaRepo", "beta"),
        OwnerScanPackage("orgs", "npm", "tools%2Fworker", "tools%2Fworker"),
    )
    assert not page.has_more


def test_listing_parser_uses_pagination_links() -> None:
    """An explicit GitHub next link continues package pagination."""

    request = PackageListingRequest("users", "example", 4, 0)
    html = """
        <a href="/users/example/packages/container/package/demo">demo</a>
        <a href="/example/repository">repository</a>
        <a rel="next" href="?tab=packages&amp;page=5">Next</a>
    """

    assert parse_package_listing_html(html, request).has_more


def test_listing_parser_continues_after_a_full_page_without_metadata() -> None:
    """A full page remains a conservative pagination fallback."""

    request = PackageListingRequest("orgs", "Example", 1, 0)
    html = "".join(
        f'<a href="/orgs/Example/packages/container/package/package-{index}">x</a>'
        for index in range(100)
    )

    page = parse_package_listing_html(html, request)

    assert len(page.packages) == 100
    assert page.has_more


@pytest.mark.parametrize(
    ("listing_request", "expected_url", "authenticated"),
    [
        (
            PackageListingRequest("users", "example", 2, 0),
            "https://github.com/example?"
            "tab=packages&visibility=public&per_page=100&page=2",
            False,
        ),
        (
            PackageListingRequest("orgs", "Example", 3, 4),
            "https://github.com/orgs/Example/packages?per_page=100&page=3",
            True,
        ),
        (
            PackageListingRequest("orgs", "Example", 1, 5),
            "https://github.com/orgs/Example/packages?"
            "visibility=private&per_page=100&page=1",
            True,
        ),
    ],
)
def test_listing_service_preserves_mode_specific_urls(
    listing_request: PackageListingRequest,
    expected_url: str,
    authenticated: bool,
) -> None:
    """The service preserves public, mixed, and private mode behavior."""

    client = FakeGitHubClient(text_values={expected_url: "<div></div>"})

    page = PackageListingService(client).fetch(listing_request)

    assert page == PackageListingPage((), False)
    assert client.text_requests == [expected_url]
    assert client.text_authentication == [authenticated]


def test_listing_404_confirms_missing_owner_before_returning_an_empty_page() -> None:
    """A listing 404 is empty only when the owner API also reports absence."""

    request = PackageListingRequest("users", "departed", 1, 0)
    url = request.url()
    client = FakeGitHubClient(
        rest_values={"users/departed": None},
        text_values={url: GitHubNotFoundError("listing not found")},
    )

    fetched = bkg_py.package_commands.fetch_package_listing_page(
        client,
        request,
    )

    assert fetched.page == PackageListingPage((), False)
    assert fetched.owner_missing
    assert not fetched.listing_unavailable
    assert client.rest_requests == ["users/departed"]


def test_listing_404_verifies_known_packages_when_the_owner_still_exists() -> None:
    """An existing owner with no listing enters package API verification."""

    request = PackageListingRequest("orgs", "available", 1, 0)
    client = FakeGitHubClient(
        rest_values={"orgs/available": {"login": "available"}},
        text_values={request.url(): GitHubNotFoundError("listing not found")},
    )

    fetched = bkg_py.package_commands.fetch_package_listing_page(client, request)

    assert fetched.page == PackageListingPage((), False)
    assert not fetched.owner_missing
    assert fetched.listing_unavailable
    assert client.rest_requests == ["orgs/available"]


def test_package_listing_cli_dispatches_shell_arguments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The public CLI accepts and returns the shell adapter representation."""

    captured: list[tuple[str, int, str, int]] = []

    def execute(
        args: argparse.Namespace,
        _application: ApplicationContext,
    ) -> object:
        captured.append((args.owner, args.page, args.marker, args.observed_at))
        page = PackageListingPage(
            (OwnerScanPackage("orgs", "container", "Repo", "demo"),), False
        )
        return bkg_py.package_commands.PackageListingWork(page, page.packages)

    monkeypatch.setattr(
        bkg_py.package_commands,
        "_execute_package_listing",
        execute,
    )
    monkeypatch.setenv("BKG_ROOT", str(tmp_path))

    status = main(
        [
            "package",
            "list-page",
            "42",
            "orgs",
            "Example",
            "3",
            "scan-1",
            "2026-06-27",
            "123456",
        ]
    )

    assert status == ExitStatus.SUCCESS
    assert captured == [("Example", 3, "scan-1", 123456)]
    assert json.loads(capsys.readouterr().out) == {
        "packages": [
            {
                "owner_type": "orgs",
                "package_type": "container",
                "repo": "Repo",
                "package": "demo",
            }
        ],
        "observed_count": 1,
        "has_more": False,
        "owner_missing": False,
        "listing_unavailable": False,
    }


def test_package_scan_cli_adopts_legacy_page_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The first database-backed scan resumes and removes legacy env keys."""

    database_path = tmp_path / "index.db"
    state_path = tmp_path / "state.env"
    marker = "batch-1:42:100:999"
    repository = DatabaseRepository(DatabaseSettings(database_path))
    repository.begin_owner_scan("42", "Example", marker, 100)
    state = StateStore(state_path)
    state.set_many({"BKG_OWNER_SCAN_42": marker, "BKG_PAGE_42": 7})
    monkeypatch.setenv("BKG_ROOT", str(tmp_path))
    monkeypatch.setenv("BKG_INDEX_DB", str(database_path))
    monkeypatch.setenv("BKG_ENV", str(state_path))

    assert main(["package", "active-scan", "42", "batch-1"]) == ExitStatus.SUCCESS
    active = json.loads(capsys.readouterr().out)
    assert active["active"] is True
    assert active["next_page"] == 1

    assert (
        main(["package", "begin-scan", "42", "Example", "batch-1", "101"])
        == ExitStatus.SUCCESS
    )
    begun = json.loads(capsys.readouterr().out)
    assert begun == {
        "marker": marker,
        "next_page": 7,
        "resumed": True,
        "discarded_legacy": False,
    }
    assert state.get("BKG_OWNER_SCAN_42") is None
    assert state.get("BKG_PAGE_42") is None

    assert (
        main(["package", "finish-page", "42", marker, "7", "102"]) == ExitStatus.SUCCESS
    )
    cursor = repository.current_owner_scan("42", "batch-1")
    assert cursor is not None
    assert cursor.next_page == 8

    observed_file = tmp_path / "observed.tsv"
    observed_file.write_text("orgs\tcontainer\tRepo\tdemo\n", encoding="utf-8")
    assert (
        main(
            [
                "package",
                "observe-refs",
                "42",
                "Example",
                marker,
                "2026-06-27",
                str(observed_file),
                "103",
            ]
        )
        == ExitStatus.SUCCESS
    )
    assert capsys.readouterr().out == "container/Repo/demo\n"
