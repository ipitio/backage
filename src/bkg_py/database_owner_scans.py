"""Transactional owner listing scans and package reconciliation."""

from __future__ import annotations

import sqlite3
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass

from .database_models import (
    OwnerScanFailure,
    OwnerScanPackage,
    OwnerScanResult,
    PackageRef,
)
from .database_support import DatabaseError

_SCANS = '"bkg_owner_scans"'
_SCAN_PACKAGES = '"bkg_owner_scan_packages"'


@dataclass(frozen=True)
class OwnerRetryPolicy:
    """Exponential owner retry limits."""

    initial_delay: int
    maximum_delay: int


@dataclass(frozen=True)
class OwnerScanTables:
    """Configured normalized table names used during reconciliation."""

    owners: str
    packages: str
    versions: str


@dataclass(frozen=True)
class OwnerScanCompletion:
    """Inputs that identify one verified complete owner scan."""

    owner_id: str
    marker: str
    scan_date: str
    completed_at: int


def _identifier(value: str) -> str:
    if "\x00" in value:
        raise DatabaseError("SQLite identifiers cannot contain NUL")
    return f'"{value.replace(chr(34), chr(34) * 2)}"'


@contextmanager
def _transaction(connection: sqlite3.Connection) -> Generator[None, None, None]:
    connection.execute("begin immediate")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    connection.commit()


def _require_active(
    connection: sqlite3.Connection,
    owner_id: str,
    marker: str,
) -> None:
    if not active(connection, owner_id, marker):
        raise DatabaseError(f"owner scan {owner_id}/{marker} is not active")


def active(
    connection: sqlite3.Connection,
    owner_id: str,
    marker: str,
) -> bool:
    """Return whether an owner scan marker is still resumable."""

    row = connection.execute(
        f"select marker, status from {_SCANS} where owner_id = ?",
        (owner_id,),
    ).fetchone()
    return row is not None and row[0] == marker and row[1] == "running"


def begin(
    connection: sqlite3.Connection,
    owner_id: str,
    owner: str,
    marker: str,
    started_at: int,
) -> None:
    """Start a fresh resumable owner listing scan."""

    with _transaction(connection):
        connection.execute(
            f"delete from {_SCAN_PACKAGES} where owner_id = ?",
            (owner_id,),
        )
        connection.execute(
            f"""
            insert into {_SCANS} (
                owner_id, owner, marker, status, started_at, updated_at,
                completed_at, failure_count, retry_after, last_error
            ) values (?, ?, ?, 'running', ?, ?, null, 0, 0, '')
            on conflict(owner_id) do update set
                owner = excluded.owner,
                marker = excluded.marker,
                status = 'running',
                started_at = excluded.started_at,
                updated_at = excluded.updated_at,
                completed_at = null,
                retry_after = 0,
                last_error = ''
            """,
            (owner_id, owner, marker, started_at, started_at),
        )


def observe(
    connection: sqlite3.Connection,
    owner_id: str,
    marker: str,
    packages: Sequence[OwnerScanPackage],
    observed_at: int,
) -> None:
    """Add package identities observed on one successfully parsed page."""

    with _transaction(connection):
        _require_active(connection, owner_id, marker)
        connection.executemany(
            f"""
            insert or ignore into {_SCAN_PACKAGES} (
                owner_id, marker, owner_type, package_type, repo, package
            ) values (?, ?, ?, ?, ?, ?)
            """,
            (
                (
                    owner_id,
                    marker,
                    package.owner_type,
                    package.package_type,
                    package.repo,
                    package.package,
                )
                for package in packages
            ),
        )
        connection.execute(
            f"update {_SCANS} set updated_at = ? where owner_id = ?",
            (observed_at, owner_id),
        )


def missing(
    connection: sqlite3.Connection,
    owner_id: str,
    marker: str,
    packages_table: str,
) -> tuple[PackageRef, ...]:
    """Return known packages absent from the staged owner listing."""

    _require_active(connection, owner_id, marker)
    packages = _identifier(packages_table)
    rows = connection.execute(
        f"""
        select distinct
            known.owner_id, known.owner_type, known.package_type,
            known.owner, known.repo, known.package
        from {packages} known
        where known.owner_id = ?
          and not exists (
            select 1
            from {_SCAN_PACKAGES} observed
            where observed.owner_id = known.owner_id
              and observed.marker = ?
              and observed.owner_type = known.owner_type
              and observed.package_type = known.package_type
              and observed.repo = known.repo
              and observed.package = known.package
          )
        order by known.owner_type, known.package_type, known.repo, known.package
        """,
        (owner_id, marker),
    ).fetchall()
    return tuple(PackageRef(*(str(value) for value in row)) for row in rows)


def _retry_after(
    failure_count: int,
    failed_at: int,
    initial_delay: int,
    maximum_delay: int,
) -> int:
    exponent = min(max(0, failure_count - 1), 30)
    delay = min(maximum_delay, initial_delay * (2**exponent))
    return failed_at + delay


def fail(
    connection: sqlite3.Connection,
    failure: OwnerScanFailure,
    retry: OwnerRetryPolicy,
) -> int:
    """Record a failed owner operation and return its next retry time."""

    with _transaction(connection):
        row = connection.execute(
            f"select marker, failure_count from {_SCANS} where owner_id = ?",
            (failure.owner_id,),
        ).fetchone()
        if failure.marker is not None and (
            row is None or str(row[0]) != failure.marker
        ):
            raise DatabaseError(
                f"owner scan {failure.owner_id}/{failure.marker} is not active"
            )
        failure_count = 1 if row is None else int(row[1]) + 1
        retry_after = _retry_after(
            failure_count,
            failure.failed_at,
            retry.initial_delay,
            retry.maximum_delay,
        )
        stored_marker = failure.marker or f"refresh:{failure.failed_at}"
        connection.execute(
            f"""
            insert into {_SCANS} (
                owner_id, owner, marker, status, started_at, updated_at,
                completed_at, failure_count, retry_after, last_error
            ) values (?, ?, ?, 'failed', ?, ?, null, ?, ?, ?)
            on conflict(owner_id) do update set
                owner = excluded.owner,
                marker = excluded.marker,
                status = 'failed',
                updated_at = excluded.updated_at,
                failure_count = excluded.failure_count,
                retry_after = excluded.retry_after,
                last_error = excluded.last_error
            """,
            (
                failure.owner_id,
                failure.owner,
                stored_marker,
                failure.failed_at,
                failure.failed_at,
                failure_count,
                retry_after,
                failure.error[:500],
            ),
        )
        connection.execute(
            f"delete from {_SCAN_PACKAGES} where owner_id = ?",
            (failure.owner_id,),
        )
    return retry_after


def clear_backoff(
    connection: sqlite3.Connection,
    owner_id: str,
    owner: str,
    completed_at: int,
) -> None:
    """Clear retry state after a successful direct package refresh."""

    with _transaction(connection):
        connection.execute(
            f"""
            insert into {_SCANS} (
                owner_id, owner, marker, status, started_at, updated_at,
                completed_at, failure_count, retry_after, last_error
            ) values (?, ?, ?, 'completed', ?, ?, ?, 0, 0, '')
            on conflict(owner_id) do update set
                owner = excluded.owner,
                status = 'completed',
                updated_at = excluded.updated_at,
                completed_at = excluded.completed_at,
                failure_count = 0,
                retry_after = 0,
                last_error = ''
            """,
            (
                owner_id,
                owner,
                f"refresh:{completed_at}",
                completed_at,
                completed_at,
                completed_at,
            ),
        )


def _delete_packages(
    connection: sqlite3.Connection,
    removed: Sequence[PackageRef],
    packages: str,
    versions: str,
    versions_table: str,
) -> None:
    for package in removed:
        legacy_table = _identifier(
            f"{versions_table}_{package.owner_type}_{package.package_type}_"
            f"{package.owner}_{package.repo}_{package.package}"
        )
        connection.execute(f"drop table if exists {legacy_table}")
        parameters = (
            package.owner_id,
            package.owner_type,
            package.package_type,
            package.owner,
            package.repo,
            package.package,
        )
        connection.execute(
            f"""
            delete from {versions}
            where owner_id = ? and owner_type = ? and package_type = ?
              and owner = ? and repo = ? and package = ?
            """,
            parameters,
        )
        connection.execute(
            f"""
            delete from {packages}
            where owner_id = ? and owner_type = ? and package_type = ?
              and owner = ? and repo = ? and package = ?
            """,
            parameters,
        )


def _scan_outcome(
    pending_count: int,
    previous_failure_count: int,
    completed_at: int,
    retry: OwnerRetryPolicy,
) -> tuple[str, int, int, str]:
    if not pending_count:
        return "completed", 0, 0, ""
    failure_count = previous_failure_count + 1
    retry_after = _retry_after(
        failure_count,
        completed_at,
        retry.initial_delay,
        retry.maximum_delay,
    )
    return (
        "failed",
        failure_count,
        retry_after,
        f"{pending_count} package refreshes remain incomplete",
    )


def complete(
    connection: sqlite3.Connection,
    completion: OwnerScanCompletion,
    tables: OwnerScanTables,
    retry: OwnerRetryPolicy,
) -> OwnerScanResult:
    """Reconcile one verified complete scan and record its refresh outcome."""

    owners = _identifier(tables.owners)
    packages = _identifier(tables.packages)

    with _transaction(connection):
        removed = missing(
            connection,
            completion.owner_id,
            completion.marker,
            tables.packages,
        )
        _delete_packages(
            connection,
            removed,
            packages,
            _identifier(tables.versions),
            tables.versions,
        )

        pending_count = int(
            connection.execute(
                f"""
                select count(*)
                from {_SCAN_PACKAGES} observed
                where observed.owner_id = ?
                  and observed.marker = ?
                  and not exists (
                    select 1
                    from {packages} current
                    where current.owner_id = observed.owner_id
                      and current.owner_type = observed.owner_type
                      and current.package_type = observed.package_type
                      and current.repo = observed.repo
                      and current.package = observed.package
                      and current.date >= ?
                  )
                """,
                (
                    completion.owner_id,
                    completion.marker,
                    completion.scan_date,
                ),
            ).fetchone()[0]
        )
        scan_row = connection.execute(
            f"select owner, failure_count from {_SCANS} where owner_id = ?",
            (completion.owner_id,),
        ).fetchone()
        if scan_row is None:
            raise DatabaseError(
                f"owner scan {completion.owner_id}/{completion.marker} disappeared"
            )
        owner = str(scan_row[0])
        status, failure_count, retry_after, last_error = _scan_outcome(
            pending_count,
            int(scan_row[1]),
            completion.completed_at,
            retry,
        )

        remaining = int(
            connection.execute(
                f"select count(*) from {packages} where owner_id = ?",
                (completion.owner_id,),
            ).fetchone()[0]
        )
        if remaining == 0:
            connection.execute(
                f"""
                insert or replace into {owners} (owner_id, owner, date)
                values (?, ?, ?)
                """,
                (completion.owner_id, owner, completion.scan_date),
            )

        connection.execute(
            f"""
            update {_SCANS}
            set status = ?, updated_at = ?, completed_at = ?,
                failure_count = ?, retry_after = ?, last_error = ?
            where owner_id = ?
            """,
            (
                status,
                completion.completed_at,
                completion.completed_at,
                failure_count,
                retry_after,
                last_error,
                completion.owner_id,
            ),
        )
        connection.execute(
            f"delete from {_SCAN_PACKAGES} where owner_id = ?",
            (completion.owner_id,),
        )

    return OwnerScanResult(removed, pending_count, retry_after)


def deferred(
    connection: sqlite3.Connection,
    now: int,
) -> tuple[tuple[str, int], ...]:
    """Return failed owners whose retry time has not arrived."""

    rows = connection.execute(
        f"""
        select owner, retry_after
        from {_SCANS}
        where status = 'failed' and retry_after > ?
        order by retry_after, owner
        """,
        (now,),
    ).fetchall()
    return tuple((str(row[0]), int(row[1])) for row in rows)
