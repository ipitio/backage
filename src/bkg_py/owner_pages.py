"""Admission helpers for REST owner discovery pages."""

from __future__ import annotations

import os
import time
from collections.abc import Generator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

from .discovery import OwnerIdentity, OwnerIdentityResolver
from .state import StateStore

_OWNER_FILE_MAX_BYTES = 100_000_000


@dataclass(frozen=True)
class OwnerPageAdmissionConfig:
    """Files and state used while admitting REST owner discovery pages."""

    state: StateStore
    owners_path: Path
    packages_all_path: Path
    lock_poll_interval: float = 0.05
    owner_file_max_bytes: int = _OWNER_FILE_MAX_BYTES


@dataclass(frozen=True)
class OwnerPageAdmissionResult:
    """Result of admitting one REST owner discovery page."""

    admitted_count: int
    owners_count: int
    has_more: bool
    requested_logins: tuple[str, ...] = ()


def admit_owner_page(
    resolver: OwnerIdentityResolver,
    config: OwnerPageAdmissionConfig,
    page_number: int,
    per_page: int,
) -> OwnerPageAdmissionResult:
    """Fetch and admit one REST owner discovery page."""

    last_id = config.state.get_int("BKG_LAST_SCANNED_ID", 0)
    page = resolver.owner_page(page_number, last_id=last_id, per_page=per_page)
    package_owners = _package_owners(config.packages_all_path)
    admitted_count = 0
    requested_logins: list[str] = []

    with _owners_lock(config):
        owner_lines = config.owners_path.read_text(encoding="utf-8").splitlines()
        known_owner_logins = {
            line.rsplit("/", maxsplit=1)[-1] for line in owner_lines if line.strip()
        }
        for owner in page.owners:
            admitted, requested_login = _admit_owner(
                owner,
                resolver,
                config,
                package_owners,
                known_owner_logins,
            )
            admitted_count += admitted
            if requested_login is not None:
                requested_logins.append(requested_login)

    return OwnerPageAdmissionResult(
        admitted_count=admitted_count,
        owners_count=len(page.owners),
        has_more=page.has_more(per_page),
        requested_logins=tuple(requested_logins),
    )


@contextmanager
def _owners_lock(config: OwnerPageAdmissionConfig) -> Generator[None, None, None]:
    config.owners_path.parent.mkdir(parents=True, exist_ok=True)
    config.owners_path.touch(exist_ok=True)
    lock_path = Path(f"{config.owners_path}.lock")
    while True:
        try:
            os.link(config.owners_path, lock_path)
            break
        except FileExistsError:
            time.sleep(config.lock_poll_interval)
        except FileNotFoundError:
            config.owners_path.touch(exist_ok=True)
            time.sleep(config.lock_poll_interval)

    try:
        yield
    finally:
        with suppress(FileNotFoundError):
            lock_path.unlink()


def _package_owners(path: Path) -> set[str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return set()
    owners: set[str] = set()
    for line in lines:
        fields = line.split("|")
        if len(fields) > 1 and fields[1]:
            owners.add(fields[1])
    return owners


def _admit_owner(
    owner: dict[str, object],
    resolver: OwnerIdentityResolver,
    config: OwnerPageAdmissionConfig,
    package_owners: set[str],
    known_owner_logins: set[str],
) -> tuple[int, str | None]:
    identity = _rest_page_owner_identity(owner)
    if identity is None:
        return 0, None
    resolver.cache.cache(identity.ref)
    if identity.login in package_owners:
        _advance_last_scanned_id(config.state, identity.owner_id)
        return 0, None

    appended = False
    if identity.login not in known_owner_logins:
        with config.owners_path.open("a", encoding="utf-8") as file:
            file.write(f"{identity.ref}\n")
        known_owner_logins.add(identity.login)
        appended = True

    if config.owners_path.stat().st_size >= config.owner_file_max_bytes:
        if appended:
            _remove_last_owner_line(config.owners_path)
            known_owner_logins.discard(identity.login)
        return 0, None

    _advance_last_scanned_id(config.state, identity.owner_id)
    return (1 if appended else 0), identity.login


def _advance_last_scanned_id(state: StateStore, owner_id: str) -> None:
    try:
        parsed_owner_id = int(owner_id)
    except ValueError:
        return
    last_id = state.get_int("BKG_LAST_SCANNED_ID", 0)
    if parsed_owner_id > last_id:
        state.set("BKG_LAST_SCANNED_ID", parsed_owner_id)


def _remove_last_owner_line(path: Path) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text(
        "".join(f"{line}\n" for line in lines[:-1]),
        encoding="utf-8",
    )


def _rest_page_owner_identity(owner: dict[str, object]) -> OwnerIdentity | None:
    owner_id = _positive_id(owner.get("id"))
    login = owner.get("login")
    if owner_id is None or not isinstance(login, str) or not login:
        return None
    return OwnerIdentity(str(owner_id), login)


def _positive_id(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None
