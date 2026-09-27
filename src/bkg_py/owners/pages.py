"""Admission helpers for REST owner discovery pages."""

import csv
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path

from ..database.support import DatabaseError
from ..database.values import package_work_item
from ..discovery import DiscoveryError, OwnerIdentity, OwnerIdentityResolver
from ..discovery.authenticated import owner_ref_login
from ..locking import FileLockOptions, advisory_file_lock
from ..runtime_names import StateKey
from ..state import StateStore

_OWNER_FILE_MAX_BYTES = 100_000_000


@dataclass(frozen=True)
class OwnerPageAdmissionConfig:
    """Files and state used while admitting REST owner discovery pages."""

    state: StateStore
    owners_path: Path
    packages_all_path: Path
    owner_file_max_bytes: int = _OWNER_FILE_MAX_BYTES
    lock_options: FileLockOptions = field(default_factory=FileLockOptions)


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
    per_page: int,
) -> OwnerPageAdmissionResult:
    """Fetch and admit one REST owner discovery page."""

    package_owners = _package_owners(config.packages_all_path)
    last_id = config.state.get_int(StateKey.LAST_SCANNED_ID, 0)
    page = resolver.owner_page(last_id=last_id, per_page=per_page)
    identities = page.owners
    resolver.cache.cache_many(identity.ref for identity in identities)

    with _owners_lock(config):
        owner_lines = config.owners_path.read_text(encoding="utf-8").splitlines()
        known_owner_logins = {
            owner_ref_login(line).casefold() for line in owner_lines if line.strip()
        }
        admitted_count, requested_logins, complete = _admit_identities(
            identities,
            config,
            package_owners,
            known_owner_logins,
        )

    if complete:
        advanced_id = page.next_since or max(
            (int(identity.owner_id) for identity in identities), default=last_id
        )
        if advanced_id > last_id:
            config.state.set(StateKey.LAST_SCANNED_ID, advanced_id)

    return OwnerPageAdmissionResult(
        admitted_count=admitted_count,
        owners_count=len(page.owners),
        has_more=complete and page.next_since is not None,
        requested_logins=requested_logins,
    )


def _owners_lock(config: OwnerPageAdmissionConfig) -> AbstractContextManager[None]:
    config.owners_path.parent.mkdir(parents=True, exist_ok=True)
    config.owners_path.touch(exist_ok=True)
    return advisory_file_lock(
        config.owners_path,
        legacy_lock_path=Path(f"{config.owners_path}.lock"),
        options=config.lock_options,
    )


def _package_owners(path: Path) -> set[str]:
    try:
        with path.open(encoding="utf-8", newline="") as file:
            reader = csv.reader(file, delimiter="|", strict=True)
            owners: set[str] = set()
            try:
                for row in reader:
                    if not row:
                        continue
                    owner = package_work_item(row).package_ref.owner
                    if owner:
                        owners.add(owner.casefold())
            except (csv.Error, DatabaseError) as error:
                raise DiscoveryError(
                    f"invalid package work item at {path}:{reader.line_num}: {error}"
                ) from error
            return owners
    except FileNotFoundError:
        return set()


def _admit_owner(
    identity: OwnerIdentity,
    config: OwnerPageAdmissionConfig,
    package_owners: set[str],
    known_owner_logins: set[str],
) -> tuple[int, str | None, bool]:
    login_key = identity.login.casefold()
    if login_key in package_owners:
        return 0, None, True
    if login_key in known_owner_logins:
        return 0, identity.login, True

    line = f"{identity.ref}\n".encode()
    with config.owners_path.open("a+b") as file:
        size = file.tell()
        if size:
            file.seek(size - 1)
            if file.read(1) != b"\n":
                line = b"\n" + line
        if size + len(line) > config.owner_file_max_bytes:
            return 0, None, False
        file.write(line)
    known_owner_logins.add(login_key)
    return 1, identity.login, True


def _admit_identities(
    identities: tuple[OwnerIdentity, ...],
    config: OwnerPageAdmissionConfig,
    package_owners: set[str],
    known_owner_logins: set[str],
) -> tuple[int, tuple[str, ...], bool]:
    admitted_count = 0
    requested_logins: list[str] = []
    for identity in identities:
        admitted, requested_login, complete = _admit_owner(
            identity,
            config,
            package_owners,
            known_owner_logins,
        )
        admitted_count += admitted
        if requested_login is not None:
            requested_logins.append(requested_login)
        if not complete:
            return admitted_count, tuple(requested_logins), False
    return admitted_count, tuple(requested_logins), True
