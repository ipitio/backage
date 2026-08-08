"""Tests for the side-by-side normalized history measurement."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from bkg_py.database.history_layout import measure_history_layout
from bkg_py.database.support import DatabaseError

from .repository_support import create_normalized_version_table


def _source(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        create_normalized_version_table(connection)
        connection.execute(
            """
            create index idx_bkg_versions_package_date
            on versions (owner_id, package_type, repo, package, date)
            """
        )
        connection.execute("create index idx_bkg_versions_date on versions (date)")
        rows = [
            (
                str(package_number // 2),
                "orgs",
                "container",
                f"owner-{package_number // 2}",
                f"repo-{package_number}",
                f"package-{package_number}",
                str(version_number),
                f"sha256:{package_number:02d}{version_number:02d}",
                100 + version_number,
                1000 + day,
                100 + day,
                10 + day,
                day,
                f"2026-08-{day + 1:02d}",
                "latest" if version_number == 3 else "",
            )
            for package_number in range(3)
            for version_number in range(4)
            for day in range(3)
        ]
        connection.executemany(
            """
            insert into versions values (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            rows,
        )


def test_measurement_preserves_history_queries_and_source(tmp_path: Path) -> None:
    """The candidate must preserve reads without mutating the source."""

    source = tmp_path / "source.db"
    candidate = tmp_path / "candidate.db"
    _source(source)
    source_bytes = source.read_bytes()

    measurement = measure_history_layout(source, candidate)

    assert source.read_bytes() == source_bytes
    assert measurement.version_observations == 36
    assert measurement.package_identities == 3
    assert measurement.version_identities == 12
    assert measurement.source_history_bytes > 0
    assert measurement.candidate_history_bytes > 0
    assert [query.name for query in measurement.queries] == [
        "largest-package",
        "largest-owner",
    ]
    assert [query.rows for query in measurement.queries] == [12, 24]
    with sqlite3.connect(candidate) as connection:
        tables = {
            str(row[0])
            for row in connection.execute(
                "select name from sqlite_master where type = 'table'"
            )
        }
    assert tables == {
        "bkg_history_packages",
        "bkg_history_versions",
        "bkg_history_version_observations",
    }


def test_measurement_refuses_to_replace_an_existing_output(tmp_path: Path) -> None:
    """An existing candidate path is never overwritten by a benchmark."""

    source = tmp_path / "source.db"
    candidate = tmp_path / "candidate.db"
    _source(source)
    candidate.write_text("keep", encoding="utf-8")

    with pytest.raises(DatabaseError, match="output already exists"):
        measure_history_layout(source, candidate)

    assert candidate.read_text(encoding="utf-8") == "keep"
