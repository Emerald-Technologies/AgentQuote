"""Failed-rows refinement for row accounting (plan 8.8 step 4b, Nick 2026-10-09).

- failed_row_excerpt: bounded cells of the failed source rows (each with the row above) for the
  refinement prompt. Customer values go only into the model call, never into stored evidence.
- row_accounting_rounds: how many refinement rounds have already failed row accounting for this
  candidate's lineage (walks author-refinement markers, refinement_of links).
- After MAX_ROW_ACCOUNTING_ROUNDS failed rounds the request is held for a person
  (row_accounting_review_required) with the row locations listed; no override exists.
Run: python -m pytest tests/test_row_refinement.py
"""
from __future__ import annotations

import json
from typing import Any

from .row_accounting import source_inventory

MAX_EXCERPT_ROWS = 100       # 50 failed rows plus the row above each
MAX_EXCERPT_CHARS = 40_000
HOLD_REASON = "row_accounting_review_required"


def failed_row_excerpt(source: bytes, extension: str, failure: dict[str, Any]) -> dict[str, Any]:
    """Cells of each failed row and the row above it, bounded by rows and characters."""
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
        cost = len(json.dumps(entry, ensure_ascii=False))
        if size + cost > MAX_EXCERPT_CHARS:
            truncated = True
            break
        rows.append(entry); size += cost
    return {"version": "row-refinement-excerpt.v1", "failed_row_count": failure.get("failed_row_count", 0),
            "rows": rows, "unmapped_columns": failure.get("unmapped_columns", []),
            "truncated": truncated or len(wanted) > MAX_EXCERPT_ROWS}


def refinement_instructions(excerpt: dict[str, Any], round_number: int, rounds: int) -> str:
    return (f"ROW ACCOUNTING REFINEMENT round {round_number} of {rounds}. The previous parser (supplied as "
            "previous_parser_code) failed row accounting on the source rows below. Each entry gives the "
            "sheet, the 1-based row, the failure code and that row's cells as [column, value]; "
            "context_row_above rows are supplied only so an alternate row can find its parent. Codes: "
            "ROW_UNACCOUNTED (row neither in a line nor skipped), ROW_IN_TWO_LINES, ROW_IN_LINE_AND_SKIPPED, "
            "ROW_CONTRIBUTES_NOTHING (listed in a line but no value cited from it), FIELD_NOT_LINKED, "
            "FIELD_VALUE_MISMATCH (cited cell does not hold the output value), FIELD_OUTSIDE_LINE, "
            "PAIR_ROWS_DIFFER, SKIPPED_ROW_LOOKS_LIKE_LINE (skipped row has a part number and quantity), "
            "FIELD_DERIVATION_NOT_ALLOWED. unmapped_columns are source columns with data in BOM lines that "
            "feed no field and are not in ignored_columns. Correct previous_parser_code so every row is "
            "accounted for, keep rows that already passed unchanged, and return the complete replacement. "
            "Never drop or invent a row to pass the check. Failed rows: "
            + json.dumps(excerpt, ensure_ascii=False, separators=(",", ":")))


def row_accounting_rounds(storage: Any, request_id: str, candidate_id: str, authoring_version: str,
                          list_paths: Any) -> int:
    """Failed row-accounting rounds already spent on this candidate's lineage (including itself)."""
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
    rounds, seen, current = 0, set(), candidate_id
    while current in parents and current not in seen:
        seen.add(current)
        current = parents[current]
        rounds += 1
    return rounds
