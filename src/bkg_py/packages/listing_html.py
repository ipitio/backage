"""Observe package results, count headings, and links in GitHub HTML."""

from dataclasses import dataclass, field
from html.parser import HTMLParser


@dataclass(frozen=True)
class ListingAnchor:
    """One link and its pagination relations."""

    href: str
    relations: frozenset[str]


@dataclass
class ListingMarkup:
    """Listing evidence kept separate from package identity interpretation."""

    anchors: list[ListingAnchor] = field(default_factory=list[ListingAnchor])
    listing_anchors: list[ListingAnchor] = field(default_factory=list[ListingAnchor])
    has_region: bool = False
    complete_region: bool = False
    package_count: int | None = None
    entry_count: int = 0
    empty_listing: bool = False

    @property
    def entry_anchors(self) -> list[ListingAnchor]:
        """Prefer links within the package results over global navigation."""

        return self.listing_anchors if self.has_region else self.anchors


@dataclass
class _ListingRegion:
    depth: int = 1
    blank_depth: int = 0
    heading_parts: list[str] | None = None
    package_count: int | None = None
    empty_heading: bool = False


class ListingHTMLParser(HTMLParser):
    """Collect only structural evidence, without inferring missing packages."""

    def __init__(self, owner_type: str) -> None:
        super().__init__(convert_charrefs=True)
        self.markup = ListingMarkup()
        self._container = (
            "user-packages-list" if owner_type == "users" else "org-packages"
        )
        self._region: _ListingRegion | None = None

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        values = {key.casefold(): value or "" for key, value in attrs}
        self._observe_start(tag, values)
        href = values.get("href", "")
        if tag != "a" or not href:
            return
        anchor = ListingAnchor(
            href, frozenset(values.get("rel", "").casefold().split())
        )
        self.markup.anchors.append(anchor)
        if self._region is not None:
            self.markup.listing_anchors.append(anchor)

    def _observe_start(self, tag: str, values: dict[str, str]) -> None:
        if tag == "div":
            if self._region is not None:
                self._region.depth += 1
            elif values.get("id") == self._container:
                self._region = _ListingRegion()
                self.markup.has_region = True
                self.markup.complete_region = False
        region = self._region
        if region is None:
            return
        classes = values.get("class", "").split()
        if tag == "li" and "Box-row" in classes:
            self.markup.entry_count += 1
        if tag == "div" and "blankslate" in classes:
            region.blank_depth = region.depth
        elif tag == "h3":
            region.heading_parts = []

    def handle_data(self, data: str) -> None:
        region = self._region
        if region is not None and region.heading_parts is not None:
            region.heading_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        region = self._region
        if region is None:
            return
        if tag == "h3" and region.heading_parts is not None:
            heading = " ".join("".join(region.heading_parts).split()).casefold()
            number, _, unit = heading.rpartition(" ")
            if unit in {"package", "packages"} and number.replace(",", "").isdecimal():
                region.package_count = int(number.replace(",", ""))
            region.empty_heading = region.empty_heading or (
                region.blank_depth > 0 and heading == "no results matched your search."
            )
            region.heading_parts = None
        if tag == "div":
            if region.depth == region.blank_depth:
                region.blank_depth = 0
            region.depth -= 1
            if region.depth == 0:
                self.markup.package_count = region.package_count
                self.markup.empty_listing = (
                    region.package_count == 0 and region.empty_heading
                )
                self.markup.complete_region = True
                self._region = None
