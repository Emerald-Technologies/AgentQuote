"""Failed-rows refinement for row accounting (plan 8.8 step 4b, Nick 2026-10-09).

- failed_row_excerpt: bounded cells of the failed source rows (each with the row above) for the
  refinement prompt. Customer values go only into the model call, never into stored evidence.
- row_accounting_rounds: how many refinement rounds have already failed row accounting for this
  candidate's lineage (walks author-refinement markers, refinement_of links).
- After MAX_ROW_ACCOUNTING_ROUNDS failed rounds the request is held for a person
  (row_accounting_review_required) with the row locations listed; no override exists. Only refinements
  of a row-accounting failure count as rounds; a parser crash does not (round 9, A041).
- parser_crash_message / crash_refinement_instructions: a crashed parser is corrected from its own
  code with the bounded error message (round 9).
Run: python -m pytest tests/test_row_refinement.py
"""
from __future__ import annotations

import json
from typing import Any

from .row_accounting import source_inventory

MAX_EXCERPT_ROWS = 100       # 50 failed rows plus the row above each
MAX_EXCERPT_CHARS = 40_000
HOLD_REASON = "row_accounting_review_required"


def failed_row_excerpt(source: bytes, extension: str, failure: dict[str, Any],
                       fields: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Cells of each failed row and the row above it, bounded by rows and characters.

    fields (round 4): per FIELD_VALUE_MISMATCH row, the output field and the source column the
    previous row map cited, shown as "cited" so the parser can see which link to correct.
    """
    cited: dict[tuple[str, int], list[dict[str, Any]]] = {}
    unlinked: dict[tuple[str, int], list[str]] = {}
    for item in fields or []:
        if item.get("unlinked"):
            unlinked.setdefault((item["sheet"], item["row"]), []).append(item["field"])
            continue
        cited.setdefault((item["sheet"], item["row"]), []).append(
            {"field": item["field"], "column": item["column"], "part": bool(item["part"])})
    inventory = source_inventory(source, extension)
    wanted: list[tuple[str, int]] = []
    for item in failure.get("failed_rows", []):
        for row in (item["row"] - 1, item["row"]):
            key = (item["sheet"], row)
            if row >= 1 and key not in wanted:
                wanted.append(key)
    by_row: dict[tuple[str, int], list[list[Any]]] = {}
    for (sheet, row, column), value in sorted(inventory.cells.items()):
        if (sheet, row) in wanted:
            by_row.setdefault((sheet, row), []).append([column, value])
    codes = {(item["sheet"], item["row"]): item["code"] for item in failure.get("failed_rows", [])}
    rows, size, truncated = [], 0, False
    for key in wanted[:MAX_EXCERPT_ROWS]:
        entry = {"sheet": key[0], "row": key[1], "code": codes.get(key, "context_row_above"),
                 "cells": by_row.get(key, [])}
        if key in cited:
            entry["cited"] = cited[key]
        if key in unlinked:
            entry["unlinked"] = unlinked[key]
        cost = len(json.dumps(entry, ensure_ascii=False))
        if size + cost > MAX_EXCERPT_CHARS:
            truncated = True
            break
        rows.append(entry); size += cost
    return {"version": "row-refinement-excerpt.v1", "failed_row_count": failure.get("failed_row_count", 0),
            "rows": rows, "unmapped_columns": failure.get("unmapped_columns", []),
            "pattern_findings": failure.get("pattern_findings", []),
            "truncated": truncated or len(wanted) > MAX_EXCERPT_ROWS}


def refinement_instructions(excerpt: dict[str, Any], round_number: int, rounds: int) -> str:
    return (f"ROW ACCOUNTING REFINEMENT round {round_number} of {rounds}. The previous parser (supplied as "
            "previous_parser_code) failed row accounting on the source rows below. Each entry gives the "
            "sheet, the 1-based row, the failure code and that row's cells as [column, value]; "
            "context_row_above rows are supplied only so an alternate row can find its parent. Codes: "
            "ROW_UNACCOUNTED (row neither in a line nor skipped), ROW_IN_TWO_LINES, ROW_IN_LINE_AND_SKIPPED, "
            "ROW_CONTRIBUTES_NOTHING (listed in a line but no value cited from it), FIELD_NOT_LINKED (unlinked lists "
            "the output fields the parser wrote for this line with no link: cite the source cell each value came "
            "from, or {\"derived\": reason} for a unit_of_measure or bom_notes value not copied from one cell), "
            "FIELD_VALUE_MISMATCH (cited cell does not hold the output value; cited lists each output field and "
            "the source column your row map gave for it, so compare that column's value in cells with what "
            "the parser wrote: cite the column that holds the value, or {\"derived\": reason} for a value the "
            "parser changed or added words to), FIELD_OUTSIDE_LINE, "
            "PAIR_ROWS_DIFFER, SKIPPED_ROW_LOOKS_LIKE_LINE (skipped row has a part number and quantity), "
            "SKIPPED_ROW_HOLDS_PAIR (a skipped row holds a manufacturer or MPN; such a row is usually an "
            "alternate of the line above: put it in that line's rows and carry its pair), "
            "CODE_LINK_NOT_ALLOWED (a \"code\" link is only for a manufacturer name), "
            "LOOKUP_KEY_MISMATCH (the looked-up row's key cell is not the line's part number), "
            "LOOKUP_ROW_NOT_CARRIED (a row on the lookup sheet holds a manufacturer or MPN for this part but the "
            "line does not carry it), LOOKUP_NOT_ALLOWED (part number and quantity come from the line's own rows), "
            "STRUCTURE_PARENT_MISMATCH (the parent worked out from the cited structure cells differs from the "
            "line's parent), STRUCTURE_VALUE_INVALID (the cited cell does not show a level, dotted number, "
            "indentation or parent), STRUCTURE_INCOMPLETE (some lines have a parent and others not), "
            "STRUCTURE_INCONSISTENT (lines use different kinds or columns), "
            "FIELD_DERIVATION_NOT_ALLOWED, PAIR_NOT_CARRIED (a manufacturer/MPN pair in this row is not in the "
            "output), PAIR_HALF_MISSING, COMBINED_PAIR_COUNT_MISMATCH (a combined cell splits into different "
            "name and MPN counts), COMBINED_PART_NOT_CARRIED. pattern_findings are columns: "
            "MANUFACTURER_MPN_SWAPPED (one column used as both), PAIR_COLUMNS_INCONSISTENT (a manufacturer "
            "column paired with different MPN columns), PAIR_COLUMN_IGNORED (an ignored column holds maker "
            "names). unmapped_columns are source columns with data in BOM lines that "
            "feed no field and are not in ignored_columns. Correct previous_parser_code so every row is "
            "accounted for, keep rows that already passed unchanged, and return the complete replacement. "
            "Never drop or invent a row to pass the check. Failed rows: "
            + json.dumps(excerpt, ensure_ascii=False, separators=(",", ":")))


def parser_crash_message(event: dict[str, Any]) -> str | None:
    """The bounded error message of a validation failure that was a parser crash, else None."""
    fixtures = event.get("fixtures") if isinstance(event.get("fixtures"), list) else []
    crashed = event.get("error_code") == "WORKER_PARSER_FAILED" or any(
        isinstance(item, dict) and item.get("code") == "WORKER_PARSER_FAILED" for item in fixtures)
    if not crashed:
        return None
    message = event.get("failure_message")
    return message[:500] if isinstance(message, str) and message.strip() else "parser failed without a message"


def crash_refinement_instructions(message: str) -> str:
    return ("PARSER CRASH REFINEMENT. The previous parser (supplied as previous_parser_code) stopped with this "
            "error when it ran on the source: " + json.dumps(message, ensure_ascii=False) + ". Find the cause in "
            "previous_parser_code, correct it and return the complete replacement, keeping everything that "
            "already works. Do not stop on rows or values you are unsure about: write your best reading of every "
            "row with the row map, and validation will name the exact rows to correct. Never drop or invent a "
            "row to get past the error.")


def fresh_crash_instructions(message: str) -> str:
    """A048: the prior parser and its own parent stopped with the same error, so patching is not working."""
    return ("FRESH PARSER ATTEMPT. The last two parsers both stopped with this same error when they ran on the "
            "source: " + json.dumps(message, ensure_ascii=False) + ". Correcting the previous code did not fix "
            "it, so it is not supplied: write a new parser from the start, read the table where the heading "
            "clues say it is, and do not repeat that error.")


def crash_signature(message: str) -> str:
    """The comparable part of a crash message: the error before its traceback (line numbers differ)."""
    text = message.split(" Traceback", 1)[0]
    if text.startswith("parser failed"):
        text = text.split("|", 1)[-1]
    return " ".join(text.split())[:200]


def _validation_failures(storage: Any, request_id: str, candidate_id: str) -> list[dict[str, Any]]:
    """The candidate's recorded validation-failed events (unreadable ones are skipped)."""
    root = f"candidates/events/{request_id}/{candidate_id}/"
    try:
        paths = storage.list_candidate_paths(root, 100)
    except Exception:
        return []
    events = []
    for path in paths:
        if not (path.startswith(root) and "/validation-failed-" in path and path.endswith(".json")):
            continue
        try:
            event = json.loads(storage.download_candidate(path, 1_000_000).decode("utf-8"))
        except Exception:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _row_failure_recorded(storage: Any, request_id: str, candidate_id: str) -> bool:
    """True when a validation failure of this candidate was a row-accounting failure."""
    return any(isinstance(event.get("row_accounting"), dict)
               for event in _validation_failures(storage, request_id, candidate_id))


def repeated_crash(storage: Any, request_id: str, candidate_id: str, message: str, authoring_version: str,
                   list_paths: Any) -> bool:
    """True when the candidate's refinement parent also crashed with the same error (A048)."""
    parent = refinement_parents(storage, request_id, authoring_version, list_paths).get(candidate_id)
    if parent is None:
        return False
    signature = crash_signature(message)
    return any(crash_signature(other) == signature
               for other in (parser_crash_message(event) for event in _validation_failures(storage, request_id, parent))
               if other is not None)


def refinement_parents(storage: Any, request_id: str, authoring_version: str, list_paths: Any) -> dict[str, str]:
    """Map of refined candidate to the candidate it was refined from, for one request."""
    # attempt-N.started.json names refinement_of (written by the dispatcher); attempt-N.completed.json
    # names the candidate the author job produced. Together they link a candidate to its parent.
    root = f"candidate-automation/{request_id}/author-refinement/{authoring_version}/"

    def read(path: str) -> dict[str, Any]:
        try:
            value = json.loads(storage.download_registry(path, 100_000).decode("utf-8"))
        except Exception:
            return {}
        return value if isinstance(value, dict) else {}

    parents: dict[str, str] = {}
    for path in list_paths(root):
        if not path.endswith(".completed.json"):
            continue
        child = read(path).get("candidate_id")
        parent = read(path[: -len(".completed.json")] + ".started.json").get("refinement_of")
        if isinstance(child, str) and isinstance(parent, str) and child != parent:
            parents.setdefault(child, parent)
    return parents


def row_accounting_rounds(storage: Any, request_id: str, candidate_id: str, authoring_version: str,
                          list_paths: Any) -> int:
    """Row-accounting refinement rounds already spent on this candidate's lineage.

    Each ancestor whose own validation failed row accounting counts once; an ancestor that crashed or
    failed for another reason does not (round 9: three crashes made the first row failure a hold).
    """
    parents = refinement_parents(storage, request_id, authoring_version, list_paths)
    rounds, seen, current = 0, set(), candidate_id
    while current in parents and current not in seen:
        seen.add(current)
        current = parents[current]
        if _row_failure_recorded(storage, request_id, current):
            rounds += 1
    return rounds
