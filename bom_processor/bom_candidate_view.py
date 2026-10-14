"""Stage 2 of the package plan (QuickQuote plan 8.3): a light view of every candidate BOM of one
assembly, plus code-computed column-overlap counts between the candidates.

Inputs: the stage 1 SurveyView and the ids of the candidate files (survey role 'bom', same
assembly). Output: CandidateView - per file the path, size, sheets with row/column counts, the
top rows, the bottom rows, and the header row plus the rows after it when the header sits below
the top rows; and, between every pair of files, how many distinct values each column pair shares
(counts only, never values). Every cell shown is recorded so the agent's citations can be checked.
Whole sheets are read (bounded by CANDIDATE_CELL_LIMIT) so the overlap and the column reads used
by the cross-check cover every row. No storage, network or model calls.
Run python -m pytest tests/test_bom_candidate_choice.py.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from typing import Any

from .foundry_author import _SECRET
from .package_survey import (SPREADSHEET_KINDS, SurveyView, _Unreadable, _shown_value,
                             read_top_rows)
from .semantic import _norm

VIEW_VERSION = "bom-candidate-view.v1"
TOP_ROWS = 20
BOTTOM_ROWS = 20
HEADER_ROWS_AFTER = 20
# The header guess only chooses which rows are shown; reading the layout is the agent's call.
HEADER_SCAN_ROWS = 200
HEADER_MIN_TEXT_CELLS = 3
FILE_CHAR_LIMIT = 15_000
FILE_CHAR_FLOOR = 6_000
CALL_CHAR_LIMIT = 90_000
CANDIDATE_CELL_LIMIT = 200_000
OVERLAP_MIN_SHARED = 3
OVERLAPS_PER_PAIR = 12


class CandidateViewError(ValueError):
    """Stable failure code without customer values."""


@dataclass(frozen=True)
class CandidateView:
    assembly: str
    file_ids: tuple[str, ...]
    entries: dict[str, dict[str, Any]]
    # Original values of every shown cell, keyed (sheet, row, column), per file.
    shown: dict[str, dict[tuple[str, int, int], Any]]
    # Every non-empty cell of every sheet, per file (whole-file reads for code checks only).
    cells: dict[str, dict[tuple[str, int, int], Any]]
    overlaps: list[dict[str, Any]]
    user_text: str
    sha256: str


def _compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _header_row(cells: dict[tuple[int, int], Any]) -> int | None:
    """The row with the most text cells in the scanned rows; earliest on a tie."""
    counts: dict[int, int] = {}
    for (row, _), value in cells.items():
        if row <= HEADER_SCAN_ROWS and isinstance(value, str):
            counts[row] = counts.get(row, 0) + 1
    best = max(sorted(counts), key=lambda row: counts[row], default=None)
    return best if best is not None and counts[best] >= HEADER_MIN_TEXT_CELLS else None


def _sections(cells: dict[tuple[int, int], Any]) -> tuple[int, int | None, dict[str, list[int]]]:
    rows = sorted({row for row, _ in cells})
    last = rows[-1] if rows else 0
    header = _header_row(cells)
    present = set(rows)
    top = [row for row in range(1, TOP_ROWS + 1) if row in present]
    sections = {"top": top}
    if header is not None and header > TOP_ROWS:
        sections["header"] = [row for row in range(header, header + HEADER_ROWS_AFTER + 1) if row in present]
    # Bottom rows are filled from the end so a cut keeps the last rows (totals, notes, sign-off).
    sections["bottom"] = [row for row in reversed(rows[-BOTTOM_ROWS:])]
    return last, header, sections


def _file_view(file_id: str, view: SurveyView, char_limit: int
               ) -> tuple[dict[str, Any], dict, dict]:
    item = view.files[file_id]
    survey_entry = view.entries[file_id]
    entry: dict[str, Any] = {"id": file_id, "path": survey_entry["path"], "size": item.size_bytes,
                             "kind": survey_entry["kind"]}
    if survey_entry.get("same_bytes_as"):
        entry["same_bytes_as"] = survey_entry["same_bytes_as"]
    shown: dict[tuple[str, int, int], Any] = {}
    every: dict[tuple[str, int, int], Any] = {}
    if survey_entry["kind"] not in SPREADSHEET_KINDS or item.unreadable:
        entry["unreadable"] = item.unreadable or "NOT_A_SPREADSHEET"
        return entry, shown, every
    try:
        sheets, macros = read_top_rows(survey_entry["kind"], item.data, max_row=None,
                                       cell_limit=CANDIDATE_CELL_LIMIT)
    except _Unreadable as exc:
        entry["unreadable"] = exc.code
        return entry, shown, every
    if macros:
        entry["macros_present_not_opened"] = True
    plans = []
    by_row: list[dict[int, list[list[Any]]]] = []
    for sheet in sheets:
        index_rows: dict[int, list[list[Any]]] = {}
        for (row, column), value in sorted(sheet.cells.items()):
            every[(sheet.name, row, column)] = value
            index_rows.setdefault(row, []).append([column, _shown_value(value)])
        by_row.append(index_rows)
        last, header, sections = _sections(sheet.cells)
        plans.append((sheet, {"name": _SECRET.sub("[REDACTED]", sheet.name), "rows": last,
                              "columns": max((column for _, column in sheet.cells), default=0),
                              "header_row_guess": header}, sections))
    entry["sheets"] = [meta for _, meta, _ in plans]
    used = len(_compact(entry))
    # Rows are added round-robin across every sheet and section so one wide section cannot
    # crowd out the others; rows already shown by another section are not repeated.
    chosen: list[set[int]] = [set() for _ in plans]
    queues = [(index, name, list(rows)) for index, (_, _, sections) in enumerate(plans)
              for name, rows in sections.items()]
    limited = False
    while any(queue for _, _, queue in queues):
        progressed = False
        for index, _, queue in queues:
            while queue and queue[0] in chosen[index]:
                queue.pop(0)
            if not queue:
                continue
            row = queue[0]
            cost = len(_compact([row, by_row[index][row]])) + 1
            if used + cost > char_limit:
                limited = True
                queue.clear()
                continue
            queue.pop(0)
            chosen[index].add(row)
            used += cost
            progressed = True
        if not progressed:
            break
    for index, (sheet, meta, _) in enumerate(plans):
        meta["shown_rows"] = []
        for row in sorted(chosen[index]):
            cells = by_row[index][row]
            meta["shown_rows"].append([row, cells])
            for column, _ in cells:
                shown[(sheet.name, row, column)] = sheet.cells[(row, column)]
    if limited:
        entry["view_limited"] = True
    return entry, shown, every


def column_values(cells: dict[tuple[str, int, int], Any], sheet: str, column: int,
                  first_row: int = 1, last_row: int | None = None) -> set[str]:
    """Distinct normalized values of one column over a row range (whole column by default)."""
    return {key for (name, row, col), value in cells.items()
            if name == sheet and col == column and row >= first_row
            and (last_row is None or row <= last_row) and (key := _norm(value))}


def _columns(cells: dict[tuple[str, int, int], Any]) -> dict[tuple[str, int], set[str]]:
    columns: dict[tuple[str, int], set[str]] = {}
    for (sheet, _, column), value in cells.items():
        key = _norm(value)
        if key:
            columns.setdefault((sheet, column), set()).add(key)
    return columns


def overlap_counts(file_ids: tuple[str, ...], cells: dict[str, dict]) -> list[dict[str, Any]]:
    """Shared distinct values per column pair, for every pair of files. Counts only."""
    columns = {file_id: _columns(cells[file_id]) for file_id in file_ids}
    found: list[dict[str, Any]] = []
    for index, left in enumerate(file_ids):
        for right in file_ids[index + 1:]:
            pairs = []
            for (left_sheet, left_column), left_values in columns[left].items():
                for (right_sheet, right_column), right_values in columns[right].items():
                    shared = len(left_values & right_values)
                    if shared >= OVERLAP_MIN_SHARED:
                        pairs.append((shared, left_sheet, left_column, len(left_values),
                                      right_sheet, right_column, len(right_values)))
            pairs.sort(key=lambda value: (-value[0], value[1], value[2], value[4], value[5]))
            for shared, ls, lc, ld, rs, rc, rd in pairs[:OVERLAPS_PER_PAIR]:
                found.append({"left": {"file": left, "sheet": _SECRET.sub("[REDACTED]", ls), "column": lc,
                                       "distinct": ld},
                              "right": {"file": right, "sheet": _SECRET.sub("[REDACTED]", rs), "column": rc,
                                        "distinct": rd},
                              "shared": shared})
    for number, item in enumerate(found, 1):
        item["id"] = f"O{number}"
    return found


def build_candidate_view(view: SurveyView, assembly: str, file_ids: list[str] | tuple[str, ...]
                         ) -> CandidateView:
    """All candidates of one assembly in one view; per-file size shrinks to fit one call."""
    ids = tuple(sorted(set(file_ids), key=lambda value: int(value[1:])))
    if len(ids) < 2 or len(ids) != len(file_ids) or any(file_id not in view.files for file_id in ids):
        raise CandidateViewError("CANDIDATE_FILES_INVALID")
    limit = FILE_CHAR_LIMIT
    while True:
        built = {file_id: _file_view(file_id, view, limit) for file_id in ids}
        cells = {file_id: built[file_id][2] for file_id in ids}
        overlaps = overlap_counts(ids, cells)
        body = {"version": VIEW_VERSION, "assembly": assembly, "choose": list(ids),
                "files": [built[file_id][0] for file_id in ids], "overlaps": overlaps}
        text = _compact(body)
        if len(text) <= CALL_CHAR_LIMIT:
            break
        if limit <= FILE_CHAR_FLOOR:
            # Splitting a large candidate set into several calls is not built (plan 8.3); stop loudly.
            raise CandidateViewError("CANDIDATE_VIEW_TOO_LARGE")
        limit = max(FILE_CHAR_FLOOR, limit * 2 // 3)
    digest = sha256(_compact({"view": body, "sha256": [view.files[i].sha256 for i in ids]}).encode()).hexdigest()
    return CandidateView(assembly, ids, {file_id: built[file_id][0] for file_id in ids},
                         {file_id: built[file_id][1] for file_id in ids}, cells, overlaps, text, digest)
