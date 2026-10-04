"""Repository-scoped package routes and explicit Maven install metadata."""

from dataclasses import dataclass
from html.parser import HTMLParser
from importlib import import_module
from typing import Protocol, cast
from urllib.parse import quote, unquote, urlsplit
from xml.etree.ElementTree import Element, ParseError

from ..database.models import OwnerScanPackage

_PATH_PARTS = 4


class _XmlParser(Protocol):  # pylint: disable=too-few-public-methods
    """Safe XML operation needed only when install metadata is requested."""

    def fromstring(
        self,
        text: str,
        *,
        forbid_dtd: bool,
        forbid_entities: bool,
        forbid_external: bool,
    ) -> Element:
        """Parse a dependency block without document types or entities."""

        raise NotImplementedError


@dataclass(frozen=True)
class LegacyPackageLink:
    """One repository-scoped package link on an owner's listing."""

    owner: str
    repo: str
    package_id: str

    @property
    def path(self) -> str:
        """Return the stable GitHub package route."""

        return (
            f"/{quote(self.owner, safe='')}/{quote(self.repo, safe='')}/packages/"
            f"{self.package_id}"
        )

    @property
    def url(self) -> str:
        """Return the package detail URL without signed artifact links."""

        return f"https://github.com{self.path}"


def legacy_package_link(href: str, owner: str) -> LegacyPackageLink | None:
    """Recognize a same-owner GitHub repository-scoped numeric package route."""

    parsed = urlsplit(href)
    if parsed.netloc and parsed.netloc.casefold() not in {
        "github.com",
        "www.github.com",
    }:
        return None
    parts = [unquote(part) for part in parsed.path.strip("/").split("/")]
    if len(parts) != _PATH_PARTS:
        return None
    if (
        parts[0].casefold() != owner.casefold()
        or not _safe_component(parts[1])
        or parts[2] != "packages"
    ):
        return None
    if not parts[3].isascii() or not parts[3].isdigit() or int(parts[3]) <= 0:
        return None
    return LegacyPackageLink(owner, parts[1], parts[3])


class _InstallMetadataParser(HTMLParser):
    """Read clipboard install examples, not names or registry icons."""

    def __init__(self) -> None:
        super().__init__()
        self.dependencies: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "clipboard-copy":
            return
        value = (dict(attrs).get("value") or "").strip()
        if value.startswith("<dependency>"):
            self.dependencies.append(value)


def parse_legacy_package_identity(
    html: str,
    link: LegacyPackageLink,
    owner_type: str,
) -> OwnerScanPackage | None:
    """Resolve only unambiguous Maven coordinates explicitly shown by GitHub."""

    parser = _InstallMetadataParser()
    parser.feed(html)
    parser.close()
    xml_parser = cast(_XmlParser, import_module("defusedxml.ElementTree"))
    names: set[str] = set()
    for value in parser.dependencies:
        try:
            dependency = xml_parser.fromstring(
                value, forbid_dtd=True, forbid_entities=True, forbid_external=True
            )
        except ParseError, ValueError:
            return None
        groups = dependency.findall("groupId")
        artifacts = dependency.findall("artifactId")
        if len(groups) != 1 or len(artifacts) != 1:
            return None
        group = (groups[0].text or "").strip()
        artifact = (artifacts[0].text or "").strip()
        if not _safe_component(group) or not _safe_component(artifact):
            return None
        names.add(f"{group}.{artifact}")
    if len(names) != 1:
        return None
    return OwnerScanPackage(
        owner_type,
        "maven",
        link.repo,
        quote(names.pop(), safe=""),
        source_package_id=link.package_id,
    )


def _safe_component(value: str) -> bool:
    return (
        bool(value)
        and value not in {".", ".."}
        and not any(
            character.isspace() or character in "/\\\x00" for character in value
        )
    )
