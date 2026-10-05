"""Human-readable publication metrics and decimal byte sizes."""

import math
from collections.abc import Sequence

_METRIC_UNITS = ("", "k", "M", "B", "T", "P", "E", "Z", "Y")
_SIZE_UNITS = ("", "kB", "MB", "GB", "TB", "PB", "EB", "ZB", "YB")
_HUMAN_SCALE_THRESHOLD = 999.9


def human_metric(value: int) -> str:
    """Format a metric with decimal unit suffixes and truncated precision."""

    return _human_units(value, _METRIC_UNITS, spaced=False)


def human_size(value: int) -> str:
    """Format a byte size with spaced decimal unit suffixes."""

    return _human_units(value, _SIZE_UNITS, spaced=True)


def _human_units(value: int, units: Sequence[str], *, spaced: bool) -> str:
    scaled = float(value)
    unit = 0
    while scaled > _HUMAN_SCALE_THRESHOLD and unit < len(units) - 1:
        scaled /= 1000
        unit += 1
    truncated = math.trunc(scaled * 10) / 10
    number = str(int(truncated)) if truncated.is_integer() else str(truncated)
    separator = " " if spaced and units[unit] else ""
    return f"{number}{separator}{units[unit]}"
