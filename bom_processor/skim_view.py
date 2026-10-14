"""Fixed-size skim view of a large BOM (plan 8.8 step 4c, plan section 8.4).

Used only when the complete inventory does not fit in one author call. The agent sees: the top 40
rows, the bottom 15, 20 rows spread through the middle, up to 40 code-flagged unusual rows (each
with the row above), and a profile of every column. The view size does not grow with the file:
when it is too large, the middle sample is trimmed first, then the unusual rows, never the top or
bottom. Completeness is not claimed: row accounting (4a) checks every row of the real file.
Input: source bytes and extension. Output: one JSON-serialisable dict. No I/O.
Run: python -m pytest tests/test_skim_view.py
"""
from __future__ import annotations

import json
import re
from collections import Counter
from typing import Any

from .row_accounting import source_inventory

VERSION = "authoring-source-skim.v1"
TOP_ROWS = 40
BOTTOM_ROWS = 15
MIDDLE_ROWS = 20
MAX_UNUSUAL_ROWS = 40
MAX_CHARS = 60_000
MAX_DISTINCT_EXAMPLES = 5
MAX_CELL_CHARS = 200
_NUMBER = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")


def _shape(value: Any) -> str:
    """Coarse value shape: digits become 9, letters A, everything else kept (bounded)."""
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    text = str(value).strip()[:24]
    return re.sub(r"[A-Za-z]", "A", re.sub(r"\d", "9", text))


def _numeric(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) or bool(_NUMBER.match(str(value).strip()))


def _clip(value: Any) -> Any:
    return value[:MAX_CELL_CHARS] if isinstance(value, str) else value


def build_skim_view(source: bytes, extension: str) -> dict[str, Any]:
    inventory = source_inventory(source, extension)
    sheets: list[dict[str, Any]] = []
    for name in inventory.sheets:
        cells = {(row, column): value for (sheet, row, column), value in inventory.cells.items()
                 if sheet == name and str(value).strip()}
        rows = sorted({row for row, _ in cells})
        by_row: dict[int, list[list[Any]]] = {}
        for (row, column), value in sorted(cells.items()):
            by_row.setdefault(row, []).append([column, _clip(value)])
        columns = sorted({column for _, column in cells})
        # Column profile over every row (counts and shapes, a few examples).
        profiles = []
        numeric_columns = set()
        for column in columns:
            values = [cells[(row, column)] for row in rows if (row, column) in cells]
            distinct = Counter(str(value).strip() for value in values)
            shapes = Counter(_shape(value) for value in values)
            numeric = sum(1 for value in values if _numeric(value))
            if values and numeric / len(values) >= 0.8:
                numeric_columns.add(column)
            profiles.append({"column": column, "filled": len(values), "distinct": len(distinct),
                             "numeric": numeric, "repeated_values": sum(1 for count in distinct.values() if count > 1),
                             "formula_cells": sum(1 for row in rows if (name, row, column) in inventory.formula_cells),
                             "top_shapes": [[shape, count] for shape, count in shapes.most_common(3)],
                             "examples": [_clip(value) for value, _ in distinct.most_common(MAX_DISTINCT_EXAMPLES)]})
        # Unusual rows: text in a mostly-numeric column; blank in a column almost every row fills
        # (often an alternate or a missing part number); the identity-like column repeating on
        # consecutive rows (often one line with alternates). Signals only, never rules.
        filled = {column: sum(1 for row in rows if (row, column) in cells) for column in columns}
        dense = [column for column in columns if rows and filled[column] >= 0.9 * len(rows)]
        distinct_count = {column: len({str(cells[(row, column)]).strip() for row in rows if (row, column) in cells})
                          for column in dense}
        key_column = max(dense, key=lambda column: (distinct_count[column], -column)) if dense else None
        unusual: list[tuple[int, str]] = []
        previous_key = None
        for row in rows[TOP_ROWS:]:
            reason = None
            if any((row, column) in cells and not _numeric(cells[(row, column)]) for column in numeric_columns):
                reason = "text_in_number_column"
            elif any((row, column) not in cells for column in dense):
                reason = "blank_in_main_column"
            elif key_column is not None and cells.get((row, key_column)) == previous_key:
                reason = "repeated_value_in_main_column"
            previous_key = cells.get((row, key_column)) if key_column is not None else None
            if reason:
                unusual.append((row, reason))
        top = rows[:TOP_ROWS]
        bottom = [row for row in rows[-BOTTOM_ROWS:] if row not in top]
        inner = [row for row in rows if row not in top and row not in bottom]
        step = max(1, len(inner) // MIDDLE_ROWS) if inner else 1
        middle = inner[::step][:MIDDLE_ROWS]
        flagged = [(row, reason) for row, reason in unusual if row not in top and row not in bottom][:MAX_UNUSUAL_ROWS]
        sheets.append({"name": name, "row_count": len(rows), "first_row": rows[0] if rows else None,
                       "last_row": rows[-1] if rows else None, "columns": profiles,
                       "_parts": {"top": top, "bottom": bottom, "middle": middle, "unusual": flagged},
                       "_rows": by_row})

    def render(middle_limit: int, unusual_limit: int) -> dict[str, Any]:
        out = []
        for sheet in sheets:
            parts, by_row = sheet["_parts"], sheet["_rows"]
            unusual = []
            for row, reason in parts["unusual"][:unusual_limit]:
                above = max([r for r in by_row if r < row], default=None)
                unusual.append({"row": row, "reason": reason, "cells": by_row[row],
                                "row_above": [above, by_row[above]] if above is not None else None})
            out.append({"name": sheet["name"], "row_count": sheet["row_count"], "first_row": sheet["first_row"],
                        "last_row": sheet["last_row"], "columns": sheet["columns"],
                        "top": [[row, by_row[row]] for row in parts["top"]],
                        "middle_sample": [[row, by_row[row]] for row in parts["middle"][:middle_limit]],
                        "bottom": [[row, by_row[row]] for row in parts["bottom"]],
                        "unusual_rows": unusual})
        return {"version": VERSION, "source_sha256": inventory.sha256, "coverage": "skim",
                "encoding": "rows are [row, [[column, value], ...]]; row and column are 1-based",
                "formula_cells": len(inventory.formula_cells), "merged_ranges": list(inventory.merged_ranges)[:200],
                "sheets": out,
                "note": ("This is a skim of a large file, not every row. Write the parser for the whole layout; "
                         "validation runs it on every row and fails on any row not accounted for.")}

    middle_limit, unusual_limit = MIDDLE_ROWS, MAX_UNUSUAL_ROWS
    view = render(middle_limit, unusual_limit)
    # Trim the middle sample first, then unusual rows; never the top or bottom (plan 8.4).
    while len(json.dumps(view, ensure_ascii=False, separators=(",", ":"))) > MAX_CHARS and (middle_limit or unusual_limit):
        if middle_limit:
            middle_limit = max(0, middle_limit - 5)
        else:
            unusual_limit = max(0, unusual_limit - 10)
        view = render(middle_limit, unusual_limit)
    if len(json.dumps(view, ensure_ascii=False, separators=(",", ":"))) > MAX_CHARS:
        raise ValueError("AUTHORING_SKIM_VIEW_LIMIT")
    view["trimmed"] = {"middle_sample": MIDDLE_ROWS - middle_limit, "unusual_rows_dropped": max(
        0, sum(len(sheet["_parts"]["unusual"]) for sheet in sheets) - unusual_limit * len(sheets))}
    return view
