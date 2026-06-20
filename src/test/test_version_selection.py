"""Tests for package-version candidate selection."""

from __future__ import annotations

import pytest

from bkg_py.version_selection import (
    VersionCandidate,
    VersionCandidatePage,
    VersionSelectionSettings,
    select_version_candidates,
)


def _candidates(
    start: int,
    stop: int,
    *,
    tagged: bool = False,
) -> tuple[VersionCandidate, ...]:
    return tuple(
        VersionCandidate(
            str(version_id),
            f"sha256:{version_id}",
            (f"tag-{version_id}",) if tagged else (),
        )
        for version_id in range(start, stop, -1)
    )


def test_selection_appends_older_tagged_versions_within_limits() -> None:
    """Older tagged entries extend the current untagged version window."""

    result = select_version_candidates(
        [VersionCandidatePage(_candidates(130, 100))],
        [
            VersionCandidatePage(_candidates(100, 70, tagged=True), has_more=True),
            VersionCandidatePage(_candidates(70, 65, tagged=True)),
        ],
        settings=VersionSelectionSettings(
            max_version_pages=3,
            max_tag_pages=3,
            append_tagged_limit=7,
        ),
    )

    selected = {int(version_id) for version_id in result.selected_ids}
    assert len(selected) == 37
    assert min(selected) == 94
    assert max(selected) == 130
    assert result.tag_pages_read == 2


def test_selection_promotes_tagged_candidates_from_a_later_page() -> None:
    """Known tagged entries replace provisional slots from the first page."""

    result = select_version_candidates(
        [
            VersionCandidatePage(_candidates(130, 100), has_more=True),
            VersionCandidatePage(_candidates(100, 70)),
        ],
        [VersionCandidatePage(_candidates(100, 90, tagged=True))],
        settings=VersionSelectionSettings(
            max_version_pages=2,
            max_tag_pages=1,
            append_tagged_limit=0,
        ),
    )

    selected = {int(version_id) for version_id in result.selected_ids}
    assert len(selected) == 30
    assert selected == {*range(91, 101), *range(111, 131)}
    assert result.version_pages_read == 2
    assert result.tag_pages_read == 1


@pytest.mark.parametrize(
    ("settings", "expected_minimum", "expected_version_pages", "expected_tag_pages"),
    [
        (VersionSelectionSettings(1, 1, 0), 101, 1, 1),
        (VersionSelectionSettings(2, 0, 0), 101, 2, 0),
    ],
)
def test_selection_respects_version_and_tag_page_limits(
    settings: VersionSelectionSettings,
    expected_minimum: int,
    expected_version_pages: int,
    expected_tag_pages: int,
) -> None:
    """Page limits retain the first-page window when promotion is unavailable."""

    result = select_version_candidates(
        [
            VersionCandidatePage(_candidates(130, 100), has_more=True),
            VersionCandidatePage(_candidates(100, 70)),
        ],
        [VersionCandidatePage(_candidates(100, 90, tagged=True))],
        settings=settings,
    )

    selected = {int(version_id) for version_id in result.selected_ids}
    assert len(selected) == 30
    assert min(selected) == expected_minimum
    assert result.version_pages_read == expected_version_pages
    assert result.tag_pages_read == expected_tag_pages


def test_selection_skips_existing_updates_but_counts_their_append_slots() -> None:
    """Existing rows still bound the historical append window without new work."""

    result = select_version_candidates(
        [VersionCandidatePage(_candidates(10, 5))],
        [VersionCandidatePage(_candidates(5, 1, tagged=True))],
        settings=VersionSelectionSettings(append_tagged_limit=2),
        already_updated={"10", "5"},
    )

    assert result.selected_ids == ("10", "9", "8", "7", "6", "5", "4")
    assert tuple(candidate.version_id for candidate in result.candidates) == (
        "9",
        "8",
        "7",
        "6",
        "4",
    )


def test_selection_merges_tag_data_before_submitting_a_provisional_version() -> None:
    """Tagged-page data enriches a provisional candidate before it is selected."""

    result = select_version_candidates(
        [VersionCandidatePage(_candidates(10, 3))],
        [
            VersionCandidatePage(
                (
                    VersionCandidate(
                        "4",
                        "sha256:tagged-page",
                        (" stable ", "latest,stable"),
                    ),
                )
            )
        ],
        settings=VersionSelectionSettings(append_tagged_limit=0),
    )

    selected = {candidate.version_id: candidate for candidate in result.candidates}
    assert selected["4"] == VersionCandidate(
        "4",
        "sha256:tagged-page",
        ("stable", "latest"),
    )


def test_selection_uses_one_fallback_when_no_versions_are_available() -> None:
    """An empty listing retains the package-level fallback update."""

    result = select_version_candidates([])

    assert result.used_fallback
    assert result.selected_ids == ("-1",)
    assert result.candidates == (VersionCandidate("-1", "latest"),)
    assert result.version_pages_read == 0
    assert result.tag_pages_read == 0


def test_selection_rejects_negative_limits() -> None:
    """Selection limits are non-negative before any pages are consumed."""

    with pytest.raises(ValueError, match="max_tag_pages"):
        VersionSelectionSettings(max_tag_pages=-1)
