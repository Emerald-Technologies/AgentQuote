"""Canonical parsed-BOM workbook columns (plan 8.12, Nick 2026-10-08).

Base columns, then manufacturer/MPN pairs numbered 1..N without gaps. N is at least 8 and only
larger when a line needs more pairs, so a workbook with 8 or fewer pairs is exactly the 25 columns
used before. More than MAX_MANUFACTURER_PAIRS is refused, never truncated. No dependencies on
other bom_processor modules (safe to import anywhere). Run python -m pytest tests/test_canonical_format.py.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

BASE_COLUMNS = [
    "line_number", "item_number", "item_name", "revision", "item_category",
    "unit_of_measure", "quantity", "reference_designator", "bom_notes",
]
MIN_MANUFACTURER_PAIRS = 8
MAX_MANUFACTURER_PAIRS = 50
_PAIR_COLUMN = re.compile(r"manufacturer(?:_item_number)?_([1-9][0-9]{0,2})")


class PairCountError(ValueError):
    """Stable code: MANUFACTURER_PAIR_COUNT_INVALID or MANUFACTURER_PAIR_LIMIT."""


def canonical_columns(pairs: int = MIN_MANUFACTURER_PAIRS) -> list[str]:
    """Canonical header for a workbook with the given number of pairs."""
    if type(pairs) is not int or not MIN_MANUFACTURER_PAIRS <= pairs <= MAX_MANUFACTURER_PAIRS:
        raise PairCountError("MANUFACTURER_PAIR_COUNT_INVALID")
    return BASE_COLUMNS + [column for index in range(1, pairs + 1) for column in (
        f"manufacturer_{index}", f"manufacturer_item_number_{index}")]


def canonical_pair_count(columns: Iterable[Any]) -> int | None:
    """Pair count when the columns are exactly a canonical header, else None."""
    columns = [str(column) for column in columns]
    extra = len(columns) - len(BASE_COLUMNS)
    if extra < 2 * MIN_MANUFACTURER_PAIRS or extra % 2 or extra > 2 * MAX_MANUFACTURER_PAIRS:
        return None
    pairs = extra // 2
    return pairs if columns == canonical_columns(pairs) else None


def _filled(value: Any) -> bool:
    return value is not None and not (isinstance(value, float) and value != value) and bool(str(value).strip())


def row_pair_count(rows: Iterable[Mapping[str, Any]]) -> int:
    """Pairs a workbook needs for these rows: the highest populated index, at least the minimum."""
    highest = MIN_MANUFACTURER_PAIRS
    for row in rows:
        for key, value in row.items():
            match = _PAIR_COLUMN.fullmatch(str(key))
            if match and _filled(value):
                highest = max(highest, int(match.group(1)))
    if highest > MAX_MANUFACTURER_PAIRS:
        raise PairCountError("MANUFACTURER_PAIR_LIMIT")
    return highest


def pair_indexes(row: Mapping[str, Any]) -> range:
    """1..N covering every pair index among the row's keys (at least the minimum)."""
    found = [int(match.group(1)) for key in row if (match := _PAIR_COLUMN.fullmatch(str(key)))]
    return range(1, max([MIN_MANUFACTURER_PAIRS, *found]) + 1)


# The minimum (8-pair, 25-column) header.
CANONICAL_COLUMNS = canonical_columns(MIN_MANUFACTURER_PAIRS)
