"""Persist bkg runtime state in the existing shell-readable environment file."""

import re
from collections.abc import Iterable, Mapping
from contextlib import AbstractContextManager
from pathlib import Path

from .files import atomic_text_output
from .locking import (
    FileLockOptions,
    LockDiagnostic,
    LockWaitCheck,
    advisory_file_lock,
)

_KEY_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class StateValueError(ValueError):
    """A key or value cannot be represented in bkg's persisted state format."""


def _validate_key(key: str) -> None:
    if _KEY_PATTERN.fullmatch(key) is None:
        raise StateValueError(f"invalid state key: {key!r}")


def _string_value(value: str | int) -> str:
    result = str(value)
    if "\n" in result or "\r" in result or "=" in result:
        raise StateValueError("state values cannot contain newlines or '='")
    return result


def _line_key(line: str) -> str | None:
    key, separator, _ = line.partition("=")
    if not separator or _KEY_PATTERN.fullmatch(key) is None:
        return None
    return key


def _incremented_counters(
    lines: list[str],
    increments: Mapping[str, int],
) -> dict[str, int]:
    updated: dict[str, int] = {}
    for key, amount in increments.items():
        prefix = f"{key}="
        raw_values = [
            line.split("=", maxsplit=2)[1] for line in lines if line.startswith(prefix)
        ]
        try:
            current = int("\n".join(raw_values)) if raw_values else 0
        except ValueError:
            current = 0
        updated[key] = current + amount
    return updated


class StateStore:
    """Read and atomically update the shell-readable runtime state file."""

    def __init__(
        self,
        path: Path,
        *,
        lock_poll_interval: float = 0.05,
        lock_timeout: float = 30.0,
        check_lock_wait: LockWaitCheck | None = None,
    ) -> None:
        self.path = path
        self.lock_poll_interval = lock_poll_interval
        self.lock_timeout = lock_timeout
        self.check_lock_wait = check_lock_wait
        self.lock_diagnostic: LockDiagnostic | None = None

    def configure_locking(
        self,
        *,
        check_wait: LockWaitCheck | None,
        diagnostic: LockDiagnostic | None = None,
    ) -> None:
        """Bind runtime stop and diagnostic hooks after application construction."""

        self.check_lock_wait = check_wait
        self.lock_diagnostic = diagnostic

    @property
    def _global_lock_path(self) -> Path:
        return Path(f"{self.path}.bkg-lock")

    @property
    def _legacy_global_lock_path(self) -> Path:
        return Path(f"{self.path}.lock")

    def _key_lock_path(self, key: str) -> Path:
        return Path(f"{self.path}.{key}.bkg-lock")

    def _legacy_key_lock_path(self, key: str) -> Path:
        return Path(f"{self.path}.{key}.lock")

    def _lock(
        self,
        lock_path: Path,
        legacy_lock_path: Path,
        *,
        interruptible: bool = True,
    ) -> AbstractContextManager[None]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)
        return advisory_file_lock(
            self.path,
            lock_path=lock_path,
            legacy_lock_path=legacy_lock_path,
            options=FileLockOptions(
                poll_interval=self.lock_poll_interval,
                timeout=self.lock_timeout,
                check_wait=self.check_lock_wait if interruptible else None,
                diagnostic=self.lock_diagnostic,
            ),
        )

    def _read_lines(self) -> list[str]:
        try:
            return self.path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []

    def _atomic_write(self, lines: Iterable[str]) -> None:
        retained_lines = [line for line in lines if line.strip()]
        content = "\n".join(retained_lines)
        content = f"{content}\n\n" if content else "\n"
        with atomic_text_output(self.path) as file:
            file.write(content)

    def get(self, key: str, default: str | None = None) -> str | None:
        """Return a scalar using the same value subset read by Bash."""

        _validate_key(key)
        prefix = f"{key}="
        values = [
            line.split("=", maxsplit=2)[1]
            for line in self._read_lines()
            if line.startswith(prefix)
        ]
        return "\n".join(values) if values else default

    def get_int(self, key: str, default: int = 0) -> int:
        """Return an integer value or a caller-provided fallback."""

        value = self.get(key)
        try:
            return int(value) if value is not None else default
        except ValueError:
            return default

    def snapshot(self) -> dict[str, str]:
        """Return the current valid key/value records without changing the file."""

        result: dict[str, str] = {}
        for line in self._read_lines():
            key = _line_key(line)
            if key is not None:
                result[key] = line.split("=", maxsplit=2)[1]
        return result

    def set(
        self,
        key: str,
        value: str | int,
        *,
        interruptible: bool = True,
    ) -> None:
        """Set one scalar while preserving unrelated and unrecognized records."""

        self.set_many({key: value}, interruptible=interruptible)

    def set_many(
        self,
        values: Mapping[str, str | int],
        *,
        interruptible: bool = True,
    ) -> None:
        """Set several scalar values with one locked atomic replacement."""

        self.update_many(values, interruptible=interruptible)

    def update_many(
        self,
        values: Mapping[str, str | int],
        *,
        increments: Mapping[str, int] | None = None,
        interruptible: bool = True,
    ) -> dict[str, int]:
        """Set values and increment counters in one atomic replacement."""

        normalized: dict[str, str] = {}
        for key, value in values.items():
            _validate_key(key)
            normalized[key] = _string_value(value)

        counter_changes = dict(increments or {})
        for key in counter_changes:
            _validate_key(key)
            if key in normalized:
                raise StateValueError(f"cannot set and increment the same key: {key}")
        if not normalized and not counter_changes:
            return {}

        with self._lock(
            self._global_lock_path,
            self._legacy_global_lock_path,
            interruptible=interruptible,
        ):
            lines = self._read_lines()
            updated_counters = _incremented_counters(lines, counter_changes)
            replacements = normalized | {
                key: str(value) for key, value in updated_counters.items()
            }
            retained = [line for line in lines if _line_key(line) not in replacements]
            retained.extend(f"{key}={value}" for key, value in replacements.items())
            self._atomic_write(retained)
        return updated_counters

    def delete(self, key: str) -> bool:
        """Delete one state key and report whether it existed."""

        return bool(self.delete_matching(keys=(key,)))

    def delete_matching(
        self,
        *,
        keys: Iterable[str] = (),
        prefixes: Iterable[str] = (),
    ) -> set[str]:
        """Delete exact keys and key prefixes with one atomic replacement."""

        exact_keys = set(keys)
        key_prefixes = tuple(prefixes)
        for key in exact_keys:
            _validate_key(key)
        for prefix in key_prefixes:
            _validate_key(prefix)

        deleted: set[str] = set()
        with self._lock(self._global_lock_path, self._legacy_global_lock_path):
            retained: list[str] = []
            for line in self._read_lines():
                key = _line_key(line)
                if key is not None and (
                    key in exact_keys
                    or any(key.startswith(prefix) for prefix in key_prefixes)
                ):
                    deleted.add(key)
                else:
                    retained.append(line)
            self._atomic_write(retained)
        return deleted

    def get_set(self, key: str) -> list[str]:
        """Decode a newline-backed set in its persisted insertion order."""

        return self._decode_set_value(self.get(key) or "")

    def add_to_set(self, key: str, item: str) -> bool:
        """Add a unique set item and report whether the state changed."""

        return bool(self.add_many_to_set(key, (item,)))

    def replace_set(self, key: str, items: Iterable[str]) -> tuple[str, ...]:
        """Replace an ordered set with one atomic state-file update."""

        _validate_key(key)
        unique = _normalize_set_items(items)
        self.set(key, r"\n".join(unique))
        return unique

    def add_many_to_set(self, key: str, items: Iterable[str]) -> tuple[str, ...]:
        """Add ordered unique set items with one locked atomic replacement."""

        _validate_key(key)
        requested = _normalize_set_items(items)
        if not requested:
            return ()

        with (
            self._lock(
                self._key_lock_path(key),
                self._legacy_key_lock_path(key),
            ),
            self._lock(self._global_lock_path, self._legacy_global_lock_path),
        ):
            lines = self._read_lines()
            prefix = f"{key}="
            raw_values = [
                line.split("=", maxsplit=2)[1]
                for line in lines
                if line.startswith(prefix)
            ]
            current_raw = "\n".join(raw_values)
            current = self._decode_set_value(current_raw)
            unique = list(dict.fromkeys(current))
            known = set(unique)
            added = tuple(item for item in requested if item not in known)
            if not added:
                return ()
            unique.extend(added)
            encoded = r"\n".join(unique)
            retained = [line for line in lines if not line.startswith(prefix)]
            retained.append(f"{key}={encoded}")
            self._atomic_write(retained)
        return added

    def increment(self, key: str, amount: int = 1, *, default: int = 0) -> int:
        """Atomically add to an integer state value and return the new value."""

        _validate_key(key)
        with self._lock(self._global_lock_path, self._legacy_global_lock_path):
            lines = self._read_lines()
            prefix = f"{key}="
            raw_values = [
                line.split("=", maxsplit=2)[1]
                for line in lines
                if line.startswith(prefix)
            ]
            try:
                current = int("\n".join(raw_values)) if raw_values else default
            except ValueError:
                current = default
            updated = current + amount
            retained = [line for line in lines if not line.startswith(prefix)]
            retained.append(f"{key}={updated}")
            self._atomic_write(retained)
        return updated

    @staticmethod
    def _decode_set_value(value: str) -> list[str]:
        if not value:
            return []
        value = value.removeprefix(r"\n").removesuffix(r"\n")
        value = value.replace(r"\n\n", r"\n", 1)
        return value.replace(r"\n", "\n").split("\n")


def _normalize_set_items(items: Iterable[str]) -> tuple[str, ...]:
    normalized: list[str] = []
    seen: set[str] = set()
    for item in items:
        value = _string_value(item)
        if not value or r"\n" in value:
            raise StateValueError("state set items cannot be empty or contain '\\n'")
        if value in seen:
            continue
        seen.add(value)
        normalized.append(value)
    return tuple(normalized)
