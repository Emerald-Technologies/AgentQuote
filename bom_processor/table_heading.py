"""Heading clues for the parser author (QuickQuote A048, Nick 2026-10-14).

A048's BOM has a plain heading row (row 1: Parent Part, Part Number, Revision, Description, Quantity,
Level...), yet five of six authored parsers failed to find it: each wrote its own heading search with
a slip ("part_number" tested against a dict keyed by column number) and the refinements kept it. The
source is read here instead, and the author is told where the table's heading row is and what its
headings suggest, so the parser only has to read the rows.

Headings are clues, not rules: a role is suggested only from heading words, the author decides each
column, and validation still checks every row against the source. No clue is given when no row reads
as a BOM heading (fewer than three distinct roles, or neither a part number nor a description), and
any error gives no clue, never a guess. Pure function of the source bytes.
Tests: tests/test_table_heading.py.
"""
from __future__ import annotations

import json
import re
from typing import Any

MAX_HEADING_ROW = 50
MAX_CLUE_SHEETS = 3
MIN_ROLES = 3

# Most specific first: the first matching role wins for a heading.
_ROLE_CLUES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("manufacturer part number", re.compile(r"\bMPNS?\b|\b(?:MANUFACTURERS?|MFRS?|MFG)\b.*\b(?:PART|PN|P N|NUMBER|NO)\b")),
    ("manufacturer", re.compile(r"\b(?:MANUFACTURERS?|MFRS?|MFG|MAKERS?|VENDORS?)\b")),
    ("parent part number", re.compile(r"\bPARENT\b")),
    ("indented part number", re.compile(r"\bINDENTED\b")),
    ("reference designators", re.compile(r"\bREF(?:ERENCE)? ?DES\w*\b|\bDESIGNATORS?\b")),
    ("find number", re.compile(r"\bFIND\b")),
    ("level", re.compile(r"^(?:BOM )?(?:LEVEL|LVL)$")),
    ("part number", re.compile(r"\bPART (?:NUMBER|NUM|NO)\b|^(?:PN|P N|PART|ITEM NUMBER|PART #)$")),
    ("revision", re.compile(r"^(?:REV|REVISION|REV LEVEL)$")),
    ("description", re.compile(r"\bDESC(?:RIPTION)?\b")),
    ("quantity", re.compile(r"\bQTY\b|\bQUANTITY\b")),
    ("unit of measure", re.compile(r"^(?:UOM|U M|UNITS?|UNIT OF MEASURE)$")),
)


def _words(value: Any) -> str:
    return re.sub(r"[^A-Z0-9#]+", " ", str(value).upper()).strip()


def _role(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip() or len(value) > 80:
        return None
    words = _words(value)
    return next((role for role, pattern in _ROLE_CLUES if pattern.search(words)), None)


def heading_clues(source: bytes, extension: str) -> list[dict[str, Any]]:
    """Per sheet, the row that reads most like a BOM heading row, with each heading's suggested role.

    Rows and columns are 1-based, as a spreadsheet shows them. Returns [] when nothing qualifies.
    """
    from .row_accounting import source_inventory
    try:
        inventory = source_inventory(source, extension)
    except Exception:
        return []
    found = []
    for sheet in inventory.sheets:
        rows: dict[int, dict[int, Any]] = {}
        for (name, row, column), value in inventory.cells.items():
            if name == sheet and row <= MAX_HEADING_ROW:
                rows.setdefault(row, {})[column] = value
        best = None
        for row in sorted(rows):
            roles = {column: _role(value) for column, value in rows[row].items()}
            named = {role for role in roles.values() if role is not None}
            if len(named) < MIN_ROLES or not named & {"part number", "description"}:
                continue
            if best is None or len(named) > best[1]:
                best = (row, len(named), roles)
        if best is None:
            continue
        row, _, roles = best
        columns = [{"column": column, "heading": str(rows[row][column]).strip()[:60],
                    **({"suggests": roles[column]} if roles[column] else {})}
                   for column in sorted(rows[row]) if str(rows[row][column]).strip()]
        found.append({"sheet": sheet, "heading_row": row, "columns": columns})
    return found[:MAX_CLUE_SHEETS]


def heading_rule(clues: list[dict[str, Any]]) -> str | None:
    """The author rule stating the heading clues, or None when there are none."""
    if not clues:
        return None
    return ("HEADING CLUES, read from --input by the service (use them instead of searching for the table "
            "yourself): " + json.dumps(clues, ensure_ascii=False, separators=(",", ":")) + ". heading_row and "
            "column are 1-based as a spreadsheet shows them (xlrd and Python lists count from 0: subtract 1 to "
            "index). The BOM lines are the rows below heading_row on that sheet. 'suggests' is what the heading "
            "words point to, a clue, not a rule: decide each column from the data, and leave a column out of "
            "the output when it is not that field. Do not write your own heading search; if a sheet has no clue "
            "and holds no BOM lines, skip it in the row map.")
