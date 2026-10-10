"""BOM layout by column-heading row (layout fix before plan 8.8 step 6, Nick 2026-10-09, option C).

A layout is the file type, the sheet names and, on each sheet, the column-heading row found by code,
wherever it sits in the top rows. Title rows, revision blocks and part rows are not part of it, so
BOMs of one format but for different assemblies share a layout (one parser request) and an approved
parser can be reused for the next assembly's BOM.

Inputs: normalized cell text of the top rows of each sheet with real row numbers (master_registry).
Outputs: find_heading -> {"row", "headings"} or None; build_layout -> the profile's layout record;
heading_agreement -> whether a validated parser's row map read the same heading row (part B).
No model call; deterministic. A row is a heading when, within the first SCAN_ROWS rows, it has at
least MIN_HEADINGS filled cells, every filled cell contains a letter, and the next filled row has at
least two filled cells under its headings. The qualifying row with the most filled cells wins; the
earliest on a tie. When no sheet has a heading the caller keeps the old fingerprint, so a BOM the
rule cannot read is treated as its own layout, never grouped by a guess.
Run: python -m pytest tests/test_layout_heading.py
"""
from __future__ import annotations

import json
import re
from typing import Any, Mapping, Sequence

VERSION = "bom-layout.v1"
SCAN_ROWS = 30
# Rows read so the heading's next row is visible even when the heading is on row SCAN_ROWS.
READ_ROWS = SCAN_ROWS + 10
MIN_HEADINGS = 3
_LETTER = re.compile(r"[a-z]")
# Row-map sheet names for sources without named sheets (row_accounting.CSV_SHEET / HTML_SHEET).
_INVENTORY_SHEETS = {"csv": {"CSV": "Sheet1"}, "html_table_xls": {"HTML_TABLE_1": "HTMLTable"}}

Rows = Sequence[tuple[int, Sequence[str]]]


def _trim(cells: Sequence[str]) -> list[str]:
    values = [str(cell) for cell in cells]
    while values and not values[-1]:
        values.pop()
    return values


def find_heading(rows: Rows) -> dict[str, Any] | None:
    """The column-heading row of one sheet; rows are (row number, normalized cells) in order."""
    ordered = [(number, _trim(cells)) for number, cells in rows]
    best: tuple[int, int, list[str]] | None = None
    for index, (number, cells) in enumerate(ordered):
        filled = [cell for cell in cells if cell]
        if number > SCAN_ROWS or len(filled) < MIN_HEADINGS or not all(_LETTER.search(cell) for cell in filled):
            continue
        below = next((other for _, other in ordered[index + 1:] if any(other)), None)
        columns = {position for position, cell in enumerate(cells) if cell}
        if below is None or sum(1 for position, cell in enumerate(below) if cell and position in columns) < 2:
            continue
        if best is None or len(filled) > best[0]:
            best = (len(filled), number, cells)
    return None if best is None else {"row": best[1], "headings": best[2]}


def build_layout(sheets: Mapping[str, Rows]) -> dict[str, Any]:
    return {"version": VERSION, "sheets": {name: find_heading(rows) for name, rows in sheets.items()}}


def layout_key(layout: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """The part of the layout that identifies it: headings only, never row numbers. None if no heading."""
    if not isinstance(layout, Mapping) or layout.get("version") != VERSION:
        return None
    sheets = layout.get("sheets", {})
    if not any(entry for entry in sheets.values()):
        return None
    return {"version": VERSION, "headings": {name: (list(entry["headings"]) if entry else None)
                                              for name, entry in sorted(sheets.items())}}


def validated_heading(evidence: Mapping[str, Any]) -> bool:
    """True when every validated file of the evidence (single or grouped) agreed on the heading row."""
    fixtures = evidence.get("fixtures")
    grouped = isinstance(fixtures, list) and any(isinstance(item, dict) and "fixture_sha256" in item
                                                 for item in fixtures)
    records = fixtures if grouped else [evidence]
    if not records:
        return False
    for record in records:
        semantic = record.get("semantic") if isinstance(record, dict) else None
        accounting = semantic.get("row_accounting") if isinstance(semantic, dict) else None
        heading = accounting.get("layout_heading") if isinstance(accounting, dict) else None
        if not (isinstance(accounting, dict) and accounting.get("valid") is True and isinstance(heading, dict)
                and heading.get("version") == VERSION and heading.get("agreed") is True):
            return False
    return True


def heading_agreement(source: bytes, extension: str, row_map: bytes) -> dict[str, Any]:
    """Did the parser read the code-found heading row? (option C, part B).

    Agreed when every sheet with BOM lines has a code-found heading row, the row map skips that row
    as a header, and every line on that sheet lies below it. Only an agreed parser is approved with
    the heading rule; otherwise approval keeps the old rule, so disagreement never blocks anything.
    """
    from .master_registry import RegistryError, profile_bytes

    def result(agreed: bool, reason: str) -> dict[str, Any]:
        return {"version": VERSION, "agreed": agreed, "reason": reason}

    try:
        profile = profile_bytes(source, extension)
    except RegistryError:
        return result(False, "PROFILE_UNREADABLE")
    layout = (profile.layout or {}).get("sheets", {})
    names = _INVENTORY_SHEETS.get(profile.source_subtype, {})
    try:
        proposal = json.loads(row_map.decode("utf-8"))
        lines, skipped = proposal["lines"], proposal["skipped"]
    except (UnicodeError, ValueError, KeyError, TypeError):
        return result(False, "ROW_MAP_UNREADABLE")
    first_line: dict[str, int] = {}
    for line in lines:
        sheet = names.get(line["sheet"], line["sheet"])
        first_line[sheet] = min(first_line.get(sheet, line["rows"][0]), *line["rows"])
    if not first_line:
        return result(False, "NO_LINES")
    headers: dict[str, set[int]] = {}
    for entry in skipped:
        if entry["reason"] == "header":
            sheet = names.get(entry["sheet"], entry["sheet"])
            for first, last in entry["rows"]:
                headers.setdefault(sheet, set()).update(range(first, min(last, READ_ROWS) + 1))
    for sheet, first in sorted(first_line.items()):
        heading = layout.get(sheet)
        if not heading:
            return result(False, "NO_HEADING_FOUND")
        if heading["row"] not in headers.get(sheet, set()):
            return result(False, "HEADING_NOT_SKIPPED_AS_HEADER")
        if first <= heading["row"]:
            return result(False, "LINE_ABOVE_HEADING")
    return result(True, "AGREED")
