"""Normalize owner references shared by discovery and queue consumers."""

import re
from collections.abc import Iterable

_IGNORED_OWNER_PATH = re.compile(
    r"^(?:.*/)*(?:solutions|sponsors|enterprise|premium-support)$"
)


def normalize_owner_lines(lines: Iterable[str]) -> tuple[str, ...]:
    """Normalize and de-duplicate owner values accepted by discovery."""

    owners: list[str] = []
    seen: set[str] = set()
    for line in lines:
        owner = line.replace('"', "").strip()
        if (
            not owner
            or owner == "0/"
            or owner.startswith("null/")
            or _IGNORED_OWNER_PATH.fullmatch(owner) is not None
            or owner in seen
        ):
            continue
        seen.add(owner)
        owners.append(owner)
    return tuple(owners)
