"""Package-version candidate selection policy."""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable, Iterator
from dataclasses import dataclass, field

_INITIAL_COMMIT_COUNT = 5
_NUMERIC_ID_PATTERN = re.compile(r"^[0-9]+$")


@dataclass(frozen=True)
class VersionCandidate:
    """One package version available for inspection and persistence."""

    version_id: str
    name: str
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class VersionCandidatePage:
    """One fetched version-listing page and its continuation state."""

    candidates: tuple[VersionCandidate, ...]
    has_more: bool = False


@dataclass(frozen=True)
class VersionSelectionSettings:
    """Limits applied while choosing package versions to inspect."""

    max_version_pages: int = 3
    max_tag_pages: int = 3
    append_tagged_limit: int = 30

    def __post_init__(self) -> None:
        for name, value in (
            ("max_version_pages", self.max_version_pages),
            ("max_tag_pages", self.max_tag_pages),
            ("append_tagged_limit", self.append_tagged_limit),
        ):
            if value < 0:
                raise ValueError(f"{name} must not be negative")


_DEFAULT_SELECTION_SETTINGS = VersionSelectionSettings()


@dataclass(frozen=True)
class VersionSelectionResult:
    """Selected versions plus page-consumption details."""

    candidates: tuple[VersionCandidate, ...]
    selected_ids: tuple[str, ...]
    version_pages_read: int
    tag_pages_read: int
    used_fallback: bool


@dataclass
class _CandidateCache:
    """Candidate source and tag data accumulated across fetched pages."""

    sources: dict[str, VersionCandidate] = field(
        default_factory=dict[str, VersionCandidate]
    )
    tags: dict[str, tuple[str, ...]] = field(default_factory=dict[str, tuple[str, ...]])
    tagged_ids: list[str] = field(default_factory=list[str])
    tagged_ids_seen: set[str] = field(default_factory=set[str])


@dataclass
class _SelectionProgress:
    """Mutable progress for one candidate-selection operation."""

    submitted: set[str] = field(default_factory=set[str])
    selected_ids: list[str] = field(default_factory=list[str])
    pending_candidates: list[VersionCandidate] = field(
        default_factory=list[VersionCandidate]
    )
    provisional_ids: list[str] = field(default_factory=list[str])
    page_ids: list[str] = field(default_factory=list[str])


def select_version_candidates(
    version_pages: Iterable[VersionCandidatePage],
    tag_pages: Iterable[VersionCandidatePage] = (),
    *,
    settings: VersionSelectionSettings = _DEFAULT_SELECTION_SETTINGS,
    already_updated: Collection[str] = (),
) -> VersionSelectionResult:
    """Choose the bounded set of versions that need detailed inspection."""

    state = _SelectionState(
        tag_pages,
        max_tag_pages=settings.max_tag_pages,
        already_updated=already_updated,
    )
    pages = iter(version_pages)
    current_page = next(pages, None)
    version_pages_read = 0

    if current_page is not None:
        version_pages_read = 1
        state.hydrate_current_page(current_page.candidates)
        state.submit_current_page(_INITIAL_COMMIT_COUNT)
        state.collect_provisional(_INITIAL_COMMIT_COUNT)
        state.resolve_provisional(settings.max_tag_pages)

        while (
            current_page.has_more
            and version_pages_read < settings.max_version_pages
            and state.has_provisional
        ):
            current_page = next(pages, None)
            if current_page is None:
                break
            version_pages_read += 1
            state.hydrate_current_page(current_page.candidates)
            state.promote_current_page(settings.max_tag_pages)

    used_fallback = not state.has_sources
    if used_fallback:
        state.store_fallback()

    state.submit_remaining_provisional()
    if used_fallback:
        state.submit("-1")

    oldest_id = state.oldest_submitted_numeric_id()
    if oldest_id is not None:
        state.append_older_tagged(oldest_id, settings.append_tagged_limit)

    return VersionSelectionResult(
        candidates=state.pending_candidates,
        selected_ids=state.selected_ids,
        version_pages_read=version_pages_read,
        tag_pages_read=state.tag_pages_read,
        used_fallback=used_fallback,
    )


class _SelectionState:
    def __init__(
        self,
        tag_pages: Iterable[VersionCandidatePage],
        *,
        max_tag_pages: int,
        already_updated: Collection[str],
    ) -> None:
        self._tag_pages: Iterator[VersionCandidatePage] = iter(tag_pages)
        self._max_tag_pages = max_tag_pages
        self._already_updated = set(already_updated)
        self._candidate_cache = _CandidateCache()
        self._progress = _SelectionProgress()
        self._tag_pages_read = 0
        self._tag_pages_exhausted = False

    @property
    def has_sources(self) -> bool:
        """Return whether any page supplied a candidate source."""

        return bool(self._candidate_cache.sources)

    @property
    def has_provisional(self) -> bool:
        """Return whether first-page slots still need resolution."""

        return bool(self._progress.provisional_ids)

    @property
    def pending_candidates(self) -> tuple[VersionCandidate, ...]:
        """Return selected candidates that have not already been updated."""

        return tuple(self._progress.pending_candidates)

    @property
    def selected_ids(self) -> tuple[str, ...]:
        """Return IDs selected in submission order."""

        return tuple(self._progress.selected_ids)

    @property
    def tag_pages_read(self) -> int:
        """Return the number of tagged-listing pages consumed."""

        return self._tag_pages_read

    def hydrate_current_page(self, candidates: Iterable[VersionCandidate]) -> None:
        """Cache one normal listing page as the current promotion window."""

        self._progress.page_ids = []
        for candidate in candidates:
            self._cache(candidate)
            self._progress.page_ids.append(candidate.version_id)

    def submit_current_page(
        self,
        commit_count: int,
        *,
        consume_provisional: bool = False,
    ) -> None:
        """Submit committed or tagged entries from the current page."""

        for index, version_id in enumerate(self._progress.page_ids):
            if consume_provisional and not self._progress.provisional_ids:
                break
            if version_id in self._progress.submitted:
                continue
            if index >= commit_count and not self._is_tagged(version_id):
                continue
            if self.submit(version_id) and consume_provisional:
                self._progress.provisional_ids.pop(0)

    def collect_provisional(self, start_index: int) -> None:
        """Capture untagged first-page slots in oldest-first order."""

        self._progress.provisional_ids = [
            version_id
            for version_id in reversed(self._progress.page_ids[start_index:])
            if version_id not in self._progress.submitted
            and not self._is_tagged(version_id)
        ]

    def resolve_provisional(self, requested_tag_pages: int) -> None:
        """Resolve provisional IDs against the bounded tagged-page cache."""

        if not self._progress.provisional_ids:
            return
        self._extend_tag_cache(self._progress.provisional_ids, requested_tag_pages)
        unresolved_ids: list[str] = []
        for version_id in self._progress.provisional_ids:
            if self._is_tagged(version_id):
                self.submit(version_id)
            else:
                unresolved_ids.append(version_id)
        self._progress.provisional_ids = unresolved_ids

    def promote_current_page(self, requested_tag_pages: int) -> None:
        """Fill provisional slots with tagged entries from a later page."""

        self.submit_current_page(0, consume_provisional=True)
        if not self._progress.provisional_ids:
            return
        self._extend_tag_cache(self._progress.page_ids, requested_tag_pages)
        self.submit_current_page(0, consume_provisional=True)

    def submit_remaining_provisional(self) -> None:
        """Submit unresolved first-page slots after bounded promotion."""

        for version_id in self._progress.provisional_ids:
            self.submit(version_id)
        self._progress.provisional_ids = []

    def store_fallback(self) -> None:
        """Store the package-level fallback candidate."""

        self._candidate_cache.sources["-1"] = VersionCandidate("-1", "latest")

    def submit(self, version_id: str) -> bool:
        """Select one cached ID and queue it when it still needs an update."""

        source = self._candidate_cache.sources.get(version_id)
        if source is None or version_id in self._progress.submitted:
            return False
        self._progress.submitted.add(version_id)
        self._progress.selected_ids.append(version_id)
        if version_id not in self._already_updated:
            self._progress.pending_candidates.append(
                VersionCandidate(
                    version_id=source.version_id,
                    name=source.name,
                    tags=self._candidate_cache.tags.get(version_id, ()),
                )
            )
        return True

    def oldest_submitted_numeric_id(self) -> int | None:
        """Return the oldest numeric ID in the selected window."""

        numeric_ids = (
            int(version_id)
            for version_id in self._progress.submitted
            if _NUMERIC_ID_PATTERN.fullmatch(version_id)
        )
        return min(numeric_ids, default=None)

    def append_older_tagged(self, older_than_id: int, append_limit: int) -> None:
        """Append bounded tagged history older than the selected window."""

        if append_limit <= 0:
            return
        appended_count = 0

        while True:
            for version_id in self._candidate_cache.tagged_ids:
                if not _NUMERIC_ID_PATTERN.fullmatch(version_id):
                    continue
                if (
                    int(version_id) >= older_than_id
                    or version_id in self._progress.submitted
                ):
                    continue
                if self.submit(version_id):
                    appended_count += 1
                    if appended_count >= append_limit:
                        return

            if self._tag_pages_exhausted or self._tag_pages_read >= self._max_tag_pages:
                return
            previous_count = self._tag_pages_read
            self._load_tag_page()
            if self._tag_pages_read == previous_count:
                return

    def _cache(self, candidate: VersionCandidate, *, tagged_page: bool = False) -> None:
        """Cache a candidate and merge its normalized tag values."""

        version_id = candidate.version_id
        self._candidate_cache.sources[version_id] = candidate
        merged_tags = _merge_tags(
            self._candidate_cache.tags.get(version_id, ()), candidate.tags
        )
        if merged_tags:
            self._candidate_cache.tags[version_id] = merged_tags
        if tagged_page and version_id not in self._candidate_cache.tagged_ids_seen:
            self._candidate_cache.tagged_ids.append(version_id)
            self._candidate_cache.tagged_ids_seen.add(version_id)

    def _is_tagged(self, version_id: str) -> bool:
        """Return whether a candidate has any cached tag."""

        return bool(self._candidate_cache.tags.get(version_id))

    def _extend_tag_cache(
        self,
        watched_ids: Iterable[str],
        requested_pages: int,
    ) -> None:
        """Read tagged pages while watched IDs remain unresolved."""

        remaining_pages = self._max_tag_pages - self._tag_pages_read
        pages_to_read = min(requested_pages, remaining_pages)
        watched = tuple(watched_ids)

        while pages_to_read > 0 and self._has_unresolved_ids(watched):
            self._load_tag_page()
            pages_to_read -= 1

    def _has_unresolved_ids(self, version_ids: Iterable[str]) -> bool:
        """Return whether tagged paging can resolve another watched ID."""

        if self._tag_pages_exhausted:
            return False
        return any(
            version_id not in self._candidate_cache.tags for version_id in version_ids
        )

    def _load_tag_page(self) -> None:
        """Consume and cache the next allowed tagged-listing page."""

        if self._tag_pages_exhausted or self._tag_pages_read >= self._max_tag_pages:
            return
        page = next(self._tag_pages, None)
        if page is None:
            self._tag_pages_exhausted = True
            return
        for candidate in page.candidates:
            self._cache(candidate, tagged_page=True)
        self._tag_pages_read += 1
        if not page.has_more:
            self._tag_pages_exhausted = True


def _merge_tags(*groups: Iterable[str]) -> tuple[str, ...]:
    merged: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for value in group:
            for tag in value.split(","):
                normalized = tag.strip()
                if not normalized or normalized in seen:
                    continue
                seen.add(normalized)
                merged.append(normalized)
    return tuple(merged)
