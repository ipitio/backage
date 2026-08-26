"""Release-period identities shared by runtime and workflow publication."""

from datetime import date


def release_tag(run_date: date) -> str:
    """Return the fortnightly GitHub release tag containing one UTC date."""

    period = (run_date.day - 1) // 14
    return f"v{run_date.year}.{run_date.month}.{period}"
