"""Select owner candidates while preserving bkg's existing queue priorities."""

from __future__ import annotations

import random
import re
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from ..runtime import resolve_executable

_IGNORED_OWNER_PATH = re.compile(
    r"^(?:.*/)*(?:solutions|sponsors|enterprise|premium-support)$"
)


def _read_lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []


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


def _owner_name(value: str) -> str:
    return value.split("/", maxsplit=1)[-1]


def _owner_key(value: str) -> str:
    return _owner_name(value).casefold()


def _available_candidates(
    candidates: list[str],
    deferred: set[str],
    requested_manual: set[str],
    limit: int,
) -> list[str]:
    return [
        candidate
        for candidate in _unique(candidates)
        if _owner_key(candidate) not in deferred
        or _owner_key(candidate) in requested_manual
    ][:limit]


def _queue_reason(
    candidate: str,
    reason_sources: tuple[tuple[str, set[str]], ...],
) -> str:
    owner = _owner_key(candidate)
    return next(
        (reason for reason, source in reason_sources if owner in source),
        "discovered",
    )


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
class OwnerQueuePaths:
    """Filesystem inputs used by owner queue selection."""

    connections_file: Path
    manual_file: Path
    index_dir: Path
    state_dir: Path


@dataclass(frozen=True)
class OwnerQueueSelector:
    """Inputs and state used to assemble the next owner candidate queue."""

    rest_first: str
    request_limit: int
    current_owner: str
    paths: OwnerQueuePaths
    include_manual: bool = True
    deferred_owners: tuple[str, ...] | None = None

    def history_owners(self) -> list[str]:
        """Return indexed owners ordered from least to most recently changed."""

        try:
            git = resolve_executable("git")
            current_result = subprocess.run(  # noqa: S603
                [
                    git,
                    "-C",
                    str(self.paths.index_dir),
                    "ls-tree",
                    "-d",
                    "--name-only",
                    "HEAD",
                ],
                check=False,
                capture_output=True,
                shell=False,
                text=True,
            )
            # Git is resolved before argv is passed without a shell.
            result = subprocess.run(  # noqa: S603
                [
                    git,
                    "-C",
                    str(self.paths.index_dir),
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

        if current_result.returncode != 0 or result.returncode != 0:
            return []
        current_owners = set(current_result.stdout.splitlines())
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
            if owner in current_owners
        ]

    def select(self, generator: random.Random | None = None) -> list[str]:
        """Return the bounded, de-duplicated owner candidate queue."""

        return [owner for owner, _reason in self.select_with_reasons(generator)]

    def select_with_reasons(
        self,
        generator: random.Random | None = None,
    ) -> list[tuple[str, str]]:
        """Return owner candidates paired with their highest-priority reason."""

        generator = generator or random.SystemRandom()
        connections = _read_lines(self.paths.connections_file)
        manual = _read_lines(self.paths.manual_file) if self.include_manual else []
        known_owners = _read_lines(self.paths.state_dir / "all_owners_in_db")
        stale = _read_lines(self.paths.state_dir / "owners_stale")
        partially_updated = _read_lines(
            self.paths.state_dir / "owners_partially_updated"
        )
        deferred = {
            _owner_key(line.split("\t", maxsplit=1)[0])
            for line in (
                self.deferred_owners
                if self.deferred_owners is not None
                else tuple(_read_lines(self.paths.state_dir / "owners_deferred"))
            )
            if line
        }
        history = self.history_owners()
        requested_manual = {_owner_key(value) for value in _requests(manual)}

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

        candidates = _requests(manual)
        if self.rest_first != "0":
            candidates.extend(remaining(stale))

        # Finish work already in the active package batch before admitting the
        # bounded discovery and history backlog.
        candidates.extend(
            _insert_into(
                remaining(partially_updated, self.request_limit),
                remaining(stale),
                generator,
            )
        )
        discovered = _unique(
            _requests(manual)
            + ([self.current_owner] if self.current_owner else [])
            + connections,
            discard_empty=True,
        )
        candidates.extend(_not_matching(known_owners, discovered))
        candidates.extend(
            _not_matching(
                known_owners,
                remaining(history),
            )
        )

        selected = _available_candidates(
            candidates,
            deferred,
            requested_manual,
            max(0, 4 * self.request_limit),
        )
        reason_sources = (
            ("manual", requested_manual),
            ("partially-updated", {_owner_key(value) for value in partially_updated}),
            ("stale", {_owner_key(value) for value in stale}),
            ("service-owner", {_owner_key(self.current_owner)}),
            ("connection", {_owner_key(value) for value in connections}),
            ("index-history", {_owner_key(value) for value in history}),
        )
        return [
            (candidate, _queue_reason(candidate, reason_sources))
            for candidate in selected
        ]
