"""Tests for explicit legacy install metadata and named-version routing."""

from html import escape

import pytest

from bkg_py.packages.legacy import (
    LegacyPackageLink,
    legacy_package_link,
    parse_legacy_package_identity,
)
from bkg_py.packages.versions.metadata import (
    VersionListingContext,
    package_detail_html_url,
    package_version_detail_html_url,
    package_versions_html_url,
)

_LINK = LegacyPackageLink("Example", "repo", "12345")


@pytest.mark.parametrize(
    "href",
    [
        "https://other.example/Example/repo/packages/12345",
        "/someone-else/repo/packages/12345",
        "/Example/repo/packages/name",
        "/Example/repo/packages/0",
        "/Example/repo%2Fother/packages/12345",
    ],
)
def test_legacy_link_rejects_foreign_or_nonpackage_routes(href: str) -> None:
    """Only a positive numeric package route on the requested owner is usable."""

    assert legacy_package_link(href, "Example") is None


def test_legacy_link_accepts_absolute_and_case_insensitive_owner_routes() -> None:
    """The canonical owner and repository survive an absolute listing link."""

    assert (
        legacy_package_link(
            "https://github.com/example/repo/packages/12345?version=1.0", "Example"
        )
        == _LINK
    )


@pytest.mark.parametrize(
    "value",
    [
        "<dependency><artifactId>library</artifactId></dependency>",
        "<dependency><groupId>org.example</groupId>"
        "<artifactId></artifactId></dependency>",
        "<dependency><groupId>org.example</groupId>"
        "<artifactId>../library</artifactId></dependency>",
        "<dependency><groupId>one</groupId><groupId>two</groupId>"
        "<artifactId>library</artifactId></dependency>",
        "<dependency><groupId>org.example</groupId><artifactId>library</dependency>",
        "<dependency><groupId>&external;</groupId>"
        "<artifactId>library</artifactId></dependency>",
    ],
)
def test_legacy_metadata_does_not_guess_missing_or_malformed_coordinates(
    value: str,
) -> None:
    """Titles, registry icons, and partial XML never establish a package identity."""

    html = (
        '<h1>org.example.library</h1><svg data-type="maven"></svg>'
        f'<clipboard-copy value="{escape(value, quote=True)}"></clipboard-copy>'
    )
    assert parse_legacy_package_identity(html, _LINK, "orgs") is None


def test_legacy_metadata_rejects_conflicting_install_examples() -> None:
    """More than one package's coordinates are ambiguous, not a first-match win."""

    html = "".join(
        '<clipboard-copy value="&lt;dependency&gt;'
        "&lt;groupId&gt;org.example&lt;/groupId&gt;"
        f"&lt;artifactId&gt;{artifact}&lt;/artifactId&gt;"
        '&lt;/dependency&gt;"></clipboard-copy>'
        for artifact in ("one", "two")
    )
    assert parse_legacy_package_identity(html, _LINK, "orgs") is None


def test_legacy_version_route_uses_the_name_not_a_fabricated_numeric_id() -> None:
    """A numeric REST ID stays the storage key while HTML takes an encoded name."""

    context = VersionListingContext(
        "orgs", "Example", "repo", "maven", "org.example.library", "12345"
    )
    assert package_detail_html_url(context) == _LINK.url
    assert package_versions_html_url(context, 2) == f"{_LINK.url}/versions?page=2"
    assert (
        package_version_detail_html_url(context, "7", version_name="1.0+build/rc&test")
        == f"{_LINK.url}?version=1.0%2Bbuild%2Frc%26test"
    )
    with pytest.raises(ValueError, match="requires its name"):
        package_version_detail_html_url(context, "7")
