"""Command-line runner for the version-history layout benchmark."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path

from .history_layout import measure_history_layout
from .support import DatabaseError


def main(arguments: Sequence[str] | None = None) -> int:
    """Measure one source database and write machine-readable results."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--versions-table", default="versions")
    options = parser.parse_args(arguments)
    try:
        measurement = measure_history_layout(
            options.source,
            options.candidate,
            versions_table=options.versions_table,
        )
    except (DatabaseError, OSError) as error:
        parser.error(str(error))
    payload = asdict(measurement)
    payload["history_bytes_saved"] = measurement.history_bytes_saved
    payload["history_reduction_percent"] = measurement.history_reduction_percent
    sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
