"""Select owner candidates while preserving bkg's existing queue priorities."""

from __future__ import annotations

import random
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .runtime import resolve_executable


def _read_lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []


def _requests(lines: list[str], count: int = 1) -> list[str]:
    if count <= 0 or not lines:
        return []
    return lines[:count] + lines[-count:]


def _unique(lines: list[str], *, discard_empty: bool = False) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for line in lines:
        if (discard_empty and not line) or line in seen:
            continue
        seen.add(line)
        result.append(line)
    return result


def _matching(patterns: list[str], lines: list[str]) -> list[str]:
    if "" in patterns:
        return list(lines)
    pattern_set = set(patterns)
    return [line for line in lines if line in pattern_set]


def _not_matching(patterns: list[str], lines: list[str]) -> list[str]:
    if "" in patterns:
        return []
    pattern_set = set(patterns)
    return [line for line in lines if line not in pattern_set]


def _insert_into(
    base: list[str],
    inserted: list[str],
    generator: random.Random,
) -> list[str]:
    if not base:
        return list(inserted)
    if not inserted:
        return list(base)

    buckets: list[list[str]] = [[] for _ in range(len(base) + 1)]
    for line in inserted:
        buckets[generator.randrange(len(buckets))].append(line)

    result: list[str] = []
    for index, line in enumerate(base):
        result.extend(buckets[index])
        result.append(line)
    result.extend(buckets[-1])
    return result


@dataclass(frozen=True)
class OwnerQueueSelector:
    """Inputs and state files used to assemble the next owner candidate queue."""

    rest_first: str
    connections_file: Path
    request_limit: int
    current_owner: str
    manual_file: Path
    index_dir: Path
    state_dir: Path

    def history_owners(self) -> list[str]:
        """Return indexed owners ordered from least to most recently changed."""

        try:
            git = resolve_executable("git")
            # Git is resolved before argv is passed without a shell.
            result = subprocess.run(  # noqa: S603
                [
                    git,
                    "-C",
                    str(self.index_dir),
                    "log",
                    "--name-only",
                    "--pretty=format:%ct",
                    "--",
                    ".",
                ],
                check=False,
                capture_output=True,
                shell=False,
                text=True,
            )
        except OSError:
            return []

        latest_timestamps: dict[str, int] = {}
        timestamp: int | None = None
        for line in result.stdout.splitlines():
            if line.isdigit():
                timestamp = int(line)
                continue
            if not line or "/" not in line or timestamp is None:
                continue
            owner = line.split("/", maxsplit=1)[0]
            latest_timestamps.setdefault(owner, timestamp)

        return [
            owner
            for owner, _ in sorted(
                latest_timestamps.items(),
                key=lambda item: (item[1], item[0]),
            )
        ]

    def select(self, generator: random.Random | None = None) -> list[str]:
        """Return the bounded, de-duplicated owner candidate queue."""

        generator = generator or random.SystemRandom()
        connections = _read_lines(self.connections_file)
        manual = _read_lines(self.manual_file)
        known_owners = _read_lines(self.state_dir / "all_owners_in_db")
        stale = _read_lines(self.state_dir / "owners_stale")
        partially_updated = _read_lines(self.state_dir / "owners_partially_updated")

        def remaining(source: list[str], extra_requests: int = 0) -> list[str]:
            result = _requests(manual)
            if self.current_owner in source:
                result.append(f"0/{self.current_owner}")
            connection_matches = _matching(source, connections)
            requested = _insert_into(
                connection_matches,
                _requests(manual, extra_requests),
                generator,
            )
            result.extend(_insert_into(source, requested, generator))
            result.extend(connection_matches)
            return result

        discovered = _unique(
            _requests(manual)
            + ([self.current_owner] if self.current_owner else [])
            + connections,
            discard_empty=True,
        )
        candidates = _not_matching(known_owners, discovered)
        candidates.extend(
            _not_matching(
                known_owners,
                remaining(self.history_owners()),
            )
        )
        if self.rest_first != "0":
            candidates.extend(remaining(stale))
        candidates.extend(
            _insert_into(
                remaining(partially_updated, self.request_limit),
                remaining(stale),
                generator,
            )
        )

        limit = max(0, 4 * self.request_limit)
        return _unique(candidates)[:limit]
