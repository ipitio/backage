"""Staged inventory reads and terminal exclusions for active owner scans."""

import sqlite3
from collections.abc import Sequence

from ..models import OwnerScanPackage
from ..support import transaction
from .scans import require_active


def observed(
    connection: sqlite3.Connection,
    owner_id: str,
    marker: str,
) -> tuple[OwnerScanPackage, ...]:
    """Return identities staged across every page of one active scan."""

    require_active(connection, owner_id, marker)
    rows = connection.execute(
        """
        select owner_type, package_type, repo, package
        from bkg_owner_scan_packages
        where owner_id = ? and marker = ?
        order by owner_type, package_type, repo, package
        """,
        (owner_id, marker),
    ).fetchall()
    return tuple(OwnerScanPackage(*(str(value) for value in row)) for row in rows)


def exclude(
    connection: sqlite3.Connection,
    owner_id: str,
    marker: str,
    packages: Sequence[OwnerScanPackage],
) -> None:
    """Remove successfully retired exclusions from the active scan inventory."""

    with transaction(connection):
        require_active(connection, owner_id, marker)
        connection.executemany(
            """
            delete from bkg_owner_scan_packages
            where owner_id = ? and marker = ? and owner_type = ?
              and package_type = ? and repo = ? and package = ?
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
