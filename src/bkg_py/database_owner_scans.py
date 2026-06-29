"""Transactional owner listing scans and package reconciliation."""

from __future__ import annotations

import sqlite3
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass

from . import database_packages
from .database_models import (
    OwnerRecord,
    OwnerRefreshPlan,
    OwnerScanCursor,
    OwnerScanFailure,
    OwnerScanPackage,
    OwnerScanPage,
    OwnerScanResult,
    OwnerScanStart,
    OwnerScanWorkSelection,
    PackageRef,
)
from .database_support import DatabaseError

_SCANS = '"bkg_owner_scans"'
_SCAN_PACKAGES = '"bkg_owner_scan_packages"'
_PACKAGE_PUBLICATIONS = '"bkg_package_publications"'


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


def current(
    connection: sqlite3.Connection,
    owner_id: str,
    batch_marker: str,
) -> OwnerScanCursor | None:
    """Return the active scan cursor when it belongs to the current batch."""

    row = connection.execute(
        f"select marker, status, next_page from {_SCANS} where owner_id = ?",
        (owner_id,),
    ).fetchone()
    if row is None or row[1] != "running":
        return None
    marker = str(row[0])
    if not marker.startswith(f"{batch_marker}:{owner_id}:"):
        return None
    return OwnerScanCursor(marker, max(1, int(row[2])), resumed=True)


def _begin(
    connection: sqlite3.Connection,
    owner_id: str,
    owner: str,
    marker: str,
    started_at: int,
) -> None:
    connection.execute(
        f"""
        insert into {_SCANS} (
            owner_id, owner, marker, status, started_at, updated_at,
            next_page, completed_at, failure_count, retry_after, last_error
        ) values (?, ?, ?, 'running', ?, ?, 1, null, 0, 0, '')
        on conflict(owner_id) do update set
            owner = excluded.owner,
            marker = excluded.marker,
            status = 'running',
            started_at = excluded.started_at,
            updated_at = excluded.updated_at,
            next_page = 1,
            completed_at = null,
            retry_after = 0,
            last_error = ''
        """,
        (owner_id, owner, marker, started_at, started_at),
    )


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
        _begin(connection, owner_id, owner, marker, started_at)


def begin_or_resume(
    connection: sqlite3.Connection,
    start: OwnerScanStart,
) -> OwnerScanCursor:
    """Resume the current batch cursor or start a replacement scan."""

    with _transaction(connection):
        cursor = current(connection, start.owner_id, start.batch_marker)
        if cursor is not None:
            next_page = cursor.next_page
            if start.legacy_marker == cursor.marker and start.legacy_page is not None:
                next_page = max(next_page, start.legacy_page, 1)
            connection.execute(
                f"update {_SCANS} set owner = ?, next_page = ? where owner_id = ?",
                (start.owner, next_page, start.owner_id),
            )
            return OwnerScanCursor(cursor.marker, next_page, resumed=True)

        marker = f"{start.batch_marker}:{start.owner_id}:{start.started_at}"
        connection.execute(
            f"delete from {_SCAN_PACKAGES} where owner_id = ?",
            (start.owner_id,),
        )
        _begin(
            connection,
            start.owner_id,
            start.owner,
            marker,
            start.started_at,
        )
        return OwnerScanCursor(marker, 1, resumed=False)


def advance_page(
    connection: sqlite3.Connection,
    page: OwnerScanPage,
) -> None:
    """Advance one page idempotently after its selected work completes."""

    with _transaction(connection):
        _require_active(connection, page.owner_id, page.marker)
        row = connection.execute(
            f"select next_page from {_SCANS} where owner_id = ?",
            (page.owner_id,),
        ).fetchone()
        if row is None:
            raise DatabaseError(f"owner scan {page.owner_id}/{page.marker} is missing")
        next_page = int(row[0])
        if next_page == page.page + 1:
            return
        if next_page != page.page:
            raise DatabaseError(
                f"owner scan {page.owner_id}/{page.marker} expected page "
                f"{next_page}, got {page.page}"
            )
        connection.execute(
            f"update {_SCANS} set next_page = ?, updated_at = ? where owner_id = ?",
            (page.page + 1, page.updated_at, page.owner_id),
        )


def write_owner(
    connection: sqlite3.Connection,
    owners_table: str,
    record: OwnerRecord,
) -> None:
    """Insert or replace one normalized owner record."""

    owners = _identifier(owners_table)
    with _transaction(connection):
        connection.execute(
            f"""
            insert or replace into {owners} (owner_id, owner, date)
            values (?, ?, ?)
            """,
            (record.owner_id, record.owner, record.date),
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
        _observe(connection, owner_id, marker, packages, observed_at)


def observe_page(
    connection: sqlite3.Connection,
    page: OwnerScanPage,
    packages: Sequence[OwnerScanPackage],
) -> None:
    """Observe a listing page only when it matches the durable cursor."""

    with _transaction(connection):
        _require_active(connection, page.owner_id, page.marker)
        row = connection.execute(
            f"select next_page from {_SCANS} where owner_id = ?",
            (page.owner_id,),
        ).fetchone()
        if row is None or int(row[0]) != page.page:
            expected = "missing" if row is None else str(row[0])
            raise DatabaseError(
                f"owner scan {page.owner_id}/{page.marker} expected page "
                f"{expected}, got {page.page}"
            )
        _observe(
            connection,
            page.owner_id,
            page.marker,
            packages,
            page.updated_at,
        )


def _observe(
    connection: sqlite3.Connection,
    owner_id: str,
    marker: str,
    packages: Sequence[OwnerScanPackage],
    observed_at: int,
) -> None:
    identities = {
        (package.owner_type, package.package_type, package.package): package
        for package in packages
    }
    package_values = tuple(identities.values())
    connection.executemany(
        f"""
        delete from {_SCAN_PACKAGES}
        where owner_id = ? and marker = ? and owner_type = ?
          and package_type = ? and package = ?
        """,
        (
            (
                owner_id,
                marker,
                package.owner_type,
                package.package_type,
                package.package,
            )
            for package in package_values
        ),
    )
    connection.executemany(
        f"""
        insert into {_SCAN_PACKAGES} (
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
            for package in package_values
        ),
    )
    connection.execute(
        f"update {_SCANS} set updated_at = ? where owner_id = ?",
        (observed_at, owner_id),
    )


def reconcile_package(
    connection: sqlite3.Connection,
    owner_id: str,
    marker: str,
    package: OwnerScanPackage,
    observed_at: int,
) -> tuple[str, ...]:
    """Replace staged repository identities for one verified package."""

    with _transaction(connection):
        _require_active(connection, owner_id, marker)
        rows = connection.execute(
            f"""
            select distinct repo
            from {_SCAN_PACKAGES}
            where owner_id = ? and marker = ? and owner_type = ?
              and package_type = ? and package = ?
            order by repo
            """,
            (
                owner_id,
                marker,
                package.owner_type,
                package.package_type,
                package.package,
            ),
        ).fetchall()
        _observe(connection, owner_id, marker, (package,), observed_at)
    return tuple(str(row[0]) for row in rows)


def packages_needing_refresh(
    connection: sqlite3.Connection,
    packages_table: str,
    selection: OwnerScanWorkSelection,
) -> tuple[OwnerScanPackage, ...]:
    """Select observed packages needing data refresh or file publication."""

    return tuple(
        package
        for package in selection.packages
        if database_packages.needs_refresh(
            connection,
            packages_table,
            PackageRef(
                selection.owner_id,
                package.owner_type,
                package.package_type,
                selection.owner,
                package.repo,
                package.package,
            ),
            selection.since,
        )
    )


def owner_refresh_plan(
    connection: sqlite3.Connection,
    packages_table: str,
    owner_id: str,
    owner: str,
    since: str,
) -> OwnerRefreshPlan:
    """Return direct package work when an owner is partially current."""

    packages = _identifier(packages_table)
    rows = connection.execute(
        f"""
        select current.owner_type, current.package_type,
               current.repo, current.package, max(current.date),
               max(case when pending.owner_id is null then 0 else 1 end),
               max(
                   case
                       when current.owner = ? and current.date >= ?
                            and pending.owner_id is null
                       then 1 else 0
                   end
               )
        from {packages} current
        left join {_PACKAGE_PUBLICATIONS} pending
          on pending.owner_id = current.owner_id
         and pending.owner_type = current.owner_type
         and pending.package_type = current.package_type
         and pending.owner = current.owner
         and pending.repo = current.repo
         and pending.package = current.package
        where current.owner_id = ?
        group by current.owner_type, current.package_type,
                 current.repo, current.package
        order by max(current.date), current.owner_type, current.package_type,
                 current.repo, current.package
        """,
        (owner, since, owner_id),
    ).fetchall()

    work = tuple(
        OwnerScanPackage(*(str(value) for value in row[:4]))
        for row in rows
        if str(row[4]) < since or bool(row[5])
    )
    return OwnerRefreshPlan(any(bool(row[6]) for row in rows), work)


def known_owner_type(
    connection: sqlite3.Connection,
    packages_table: str,
    owner_id: str,
    owner: str,
) -> str | None:
    """Return one unambiguous owner type already present in durable state."""

    packages = _identifier(packages_table)
    rows = connection.execute(
        f"""
        select distinct owner_type
        from (
            select owner_type
            from {packages}
            where owner_id = ? and owner = ? collate nocase
            union all
            select observed.owner_type
            from {_SCAN_PACKAGES} observed
            join {_SCANS} scan on scan.owner_id = observed.owner_id
            where observed.owner_id = ? and scan.owner = ? collate nocase
        )
        order by owner_type
        """,
        (owner_id, owner, owner_id, owner),
    ).fetchall()
    values = tuple(str(row[0]) for row in rows if row[0] in ("orgs", "users"))
    return values[0] if len(values) == 1 else None


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


def _missing_package_is_replaced(
    connection: sqlite3.Connection,
    package: PackageRef,
    marker: str,
    packages_table: str,
    since: str,
) -> bool:
    packages = _identifier(packages_table)
    row = connection.execute(
        f"""
        select
            exists (
                select 1
                from {_SCAN_PACKAGES} observed
                where observed.owner_id = ? and observed.marker = ?
                  and observed.owner_type = ? and observed.package_type = ?
                  and observed.package = ?
            ),
            exists (
                select 1
                from {_SCAN_PACKAGES} observed
                join {packages} current
                  on current.owner_id = observed.owner_id
                 and current.owner_type = observed.owner_type
                 and current.package_type = observed.package_type
                 and current.repo = observed.repo
                 and current.package = observed.package
                where observed.owner_id = ? and observed.marker = ?
                  and observed.owner_type = ? and observed.package_type = ?
                  and observed.package = ? and current.date >= ?
                  and not exists (
                      select 1 from {_PACKAGE_PUBLICATIONS} pending
                      where pending.owner_id = current.owner_id
                        and pending.owner_type = current.owner_type
                        and pending.package_type = current.package_type
                        and pending.owner = current.owner
                        and pending.repo = current.repo
                        and pending.package = current.package
                  )
            )
        """,
        (
            package.owner_id,
            marker,
            package.owner_type,
            package.package_type,
            package.package,
            package.owner_id,
            marker,
            package.owner_type,
            package.package_type,
            package.package,
            since,
        ),
    ).fetchone()
    if row is None:
        raise DatabaseError("failed to inspect owner scan replacement state")
    replacement_observed, replacement_published = (bool(value) for value in row)
    return not replacement_observed or replacement_published


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
        connection.execute(
            f"""
            delete from {_PACKAGE_PUBLICATIONS}
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


def _removable_missing_packages(
    connection: sqlite3.Connection,
    completion: OwnerScanCompletion,
    packages_table: str,
) -> tuple[PackageRef, ...]:
    return tuple(
        package
        for package in missing(
            connection,
            completion.owner_id,
            completion.marker,
            packages_table,
        )
        if _missing_package_is_replaced(
            connection,
            package,
            completion.marker,
            packages_table,
            completion.scan_date,
        )
    )


def _pending_scan_packages(
    connection: sqlite3.Connection,
    completion: OwnerScanCompletion,
    packages: str,
) -> tuple[OwnerScanPackage, ...]:
    rows = connection.execute(
        f"""
        select observed.owner_type, observed.package_type,
               observed.repo, observed.package
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
              and not exists (
                  select 1 from {_PACKAGE_PUBLICATIONS} pending
                  where pending.owner_id = current.owner_id
                    and pending.owner_type = current.owner_type
                    and pending.package_type = current.package_type
                    and pending.owner = current.owner
                    and pending.repo = current.repo
                    and pending.package = current.package
              )
          )
        order by observed.owner_type, observed.package_type,
                 observed.repo, observed.package
        """,
        (
            completion.owner_id,
            completion.marker,
            completion.scan_date,
        ),
    ).fetchall()
    return tuple(OwnerScanPackage(*(str(value) for value in row)) for row in rows)


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
        removed = _removable_missing_packages(
            connection,
            completion,
            tables.packages,
        )
        _delete_packages(
            connection,
            removed,
            packages,
            _identifier(tables.versions),
            tables.versions,
        )

        pending = _pending_scan_packages(connection, completion, packages)
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
            len(pending),
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

    return OwnerScanResult(removed, pending, retry_after)


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


def retire_owner(
    connection: sqlite3.Connection,
    owner: str,
    tables: OwnerScanTables,
) -> int:
    """Remove one unavailable owner's normalized, legacy, and scan state."""

    owners = _identifier(tables.owners)
    packages = _identifier(tables.packages)
    versions = _identifier(tables.versions)
    package_rows = connection.execute(
        f"""
        select distinct owner_type, package_type, owner, repo, package
        from {packages}
        where owner = ?
        """,
        (owner,),
    ).fetchall()
    legacy_tables = {
        f"{tables.versions}_{'_'.join(str(value) for value in row)}"
        for row in package_rows
    }
    deleted = 0
    with _transaction(connection):
        for table_name in legacy_tables:
            connection.execute(f"drop table if exists {_identifier(table_name)}")
        for table in (owners, packages, versions):
            cursor = connection.execute(
                f"delete from {table} where owner = ?",
                (owner,),
            )
            deleted += cursor.rowcount
        database_packages.retire_owner_publications(connection, owner)
        connection.execute(
            f"""
            delete from {_SCAN_PACKAGES}
            where owner_id in (
                select owner_id from {_SCANS} where owner = ?
            )
            """,
            (owner,),
        )
        connection.execute(
            f"delete from {_SCANS} where owner = ?",
            (owner,),
        )
    return deleted
