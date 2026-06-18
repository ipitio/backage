"""Persist bkg runtime state in the existing shell-readable environment file."""

from __future__ import annotations

import os
import re
import time
from collections.abc import Generator, Iterable, Mapping
from contextlib import contextmanager, suppress
from pathlib import Path

from .files import atomic_text_output

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


class StateStore:
    """Read and update the state file while interoperating with Bash locks."""

    def __init__(self, path: Path, *, lock_poll_interval: float = 0.05) -> None:
        self.path = path
        self.lock_poll_interval = lock_poll_interval

    @property
    def _global_lock_path(self) -> Path:
        return Path(f"{self.path}.lock")

    def _key_lock_path(self, key: str) -> Path:
        return Path(f"{self.path}.{key}.lock")

    def _wait_for_unlock(self) -> None:
        while self._global_lock_path.exists():
            time.sleep(self.lock_poll_interval)

    @contextmanager
    def _lock(self, lock_path: Path) -> Generator[None, None, None]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)
        while True:
            try:
                os.link(self.path, lock_path)
                break
            except FileExistsError:
                time.sleep(self.lock_poll_interval)
            except FileNotFoundError:
                self.path.touch(exist_ok=True)
                time.sleep(self.lock_poll_interval)

        try:
            yield
        finally:
            with suppress(FileNotFoundError):
                lock_path.unlink()

    def _read_lines(self, *, wait_for_unlock: bool = True) -> list[str]:
        if wait_for_unlock:
            self._wait_for_unlock()
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

    def set(self, key: str, value: str | int) -> None:
        """Set one scalar while preserving unrelated and unrecognized records."""

        self.set_many({key: value})

    def set_many(self, values: Mapping[str, str | int]) -> None:
        """Set several scalar values with one locked atomic replacement."""

        normalized: dict[str, str] = {}
        for key, value in values.items():
            _validate_key(key)
            normalized[key] = _string_value(value)
        if not normalized:
            return

        with self._lock(self._global_lock_path):
            lines = [
                line
                for line in self._read_lines(wait_for_unlock=False)
                if _line_key(line) not in normalized
            ]
            lines.extend(f"{key}={value}" for key, value in normalized.items())
            self._atomic_write(lines)

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
        with self._lock(self._global_lock_path):
            retained: list[str] = []
            for line in self._read_lines(wait_for_unlock=False):
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

        _validate_key(key)
        item = _string_value(item)
        if not item or r"\n" in item:
            raise StateValueError("state set items cannot be empty or contain '\\n'")

        with (
            self._lock(self._key_lock_path(key)),
            self._lock(self._global_lock_path),
        ):
            lines = self._read_lines(wait_for_unlock=False)
            prefix = f"{key}="
            raw_values = [
                line.split("=", maxsplit=2)[1]
                for line in lines
                if line.startswith(prefix)
            ]
            current_raw = "\n".join(raw_values)
            current = self._decode_set_value(current_raw)
            unique = list(dict.fromkeys(current))
            if item in unique:
                return False
            unique.append(item)
            encoded = r"\n".join(unique)
            retained = [line for line in lines if not line.startswith(prefix)]
            retained.append(f"{key}={encoded}")
            self._atomic_write(retained)
        return True

    def increment(self, key: str, amount: int = 1, *, default: int = 0) -> int:
        """Atomically add to an integer state value and return the new value."""

        _validate_key(key)
        with self._lock(self._global_lock_path):
            lines = self._read_lines(wait_for_unlock=False)
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
