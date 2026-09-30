"""Fail-closed semantic policy for Arena HTML spreadsheet exports."""
from __future__ import annotations

import hashlib
import io
import json
import math
import re
from typing import Any

import pandas as pd

from .core import CANONICAL_COLUMNS
from .master_registry import html_table_raw_rows

POLICY_VERSION = "arena-html-policy.v1"
_PAIR = re.compile(r"^(?:manufacturer|manufacturer_name)_(\d+)$")
_MPN = re.compile(r"^(?:manufacturer_item_number|manufacturer_part_number)_(\d+)$")


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _number(value: Any) -> float | None:
    text = _text(value)
    if not text:
        return None
    try:
        result = float(text)
    except (TypeError, ValueError):
        return math.nan
    return result if math.isfinite(result) else math.nan


def _key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", _text(value).lower()).strip("_")


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def verify_evidence(evidence: dict[str, Any]) -> bool:
    decisions = evidence.get("decisions")
    expected_omissions = ([{key: decision[key] for key in ("row", "item_number", "decision") if key in decision}
                           for decision in decisions if str(decision.get("decision", "")).startswith("omitted_")]
                          if isinstance(decisions, list) else None)
    return (evidence.get("version") == POLICY_VERSION and evidence.get("valid") is True
            and isinstance(decisions, list) and len(decisions) <= 100_000
            and evidence.get("decision_hash") == _digest(decisions)
            and evidence.get("omissions") == expected_omissions
            and evidence.get("included_count") == evidence.get("source_rows")
            and evidence.get("omitted_count") == len(expected_omissions))


def _headers(rows: tuple[tuple[str, ...], ...]) -> tuple[int, dict[str, int]]:
    required = {"item_number", "item_name", "item_category", "quantity"}
    for index, row in enumerate(rows):
        values = {_key(value): column for column, value in enumerate(row)}
        pairs = {int(match.group(1)) for value in values for match in (_PAIR.match(value),) if match}
        mpns = {int(match.group(1)) for value in values for match in (_MPN.match(value),) if match}
        if required <= values.keys() and pairs and mpns:
            return index, values
    return -1, {}


def validate(source: bytes, output: bytes, *, source_sha256: str, candidate_code_sha256: str,
             output_sha256: str) -> dict[str, Any]:
    """Validate an Arena source/output pair and return bounded deterministic evidence."""
    rows = html_table_raw_rows(source)
    header_index, headers = _headers(rows)
    base = {"version": POLICY_VERSION, "source_sha256": source_sha256,
            "candidate_code_sha256": candidate_code_sha256, "output_sha256": output_sha256}
    if header_index < 0:
        return {**base, "valid": False, "reason": "arena_header_family_missing", "decisions": []}
    source_rows = rows[header_index + 1:]
    source_items: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    for row_number, row in enumerate(source_rows, header_index + 2):
        values = {name: _text(row[index]) if index < len(row) else "" for name, index in headers.items()}
        if not any(values.values()):
            continue
        item = values["item_number"]
        category = values["item_category"]
        quantity = _number(values["quantity"])
        refdes = values.get("reference_designator", values.get("refdes", ""))
        pair_values = [(index, values.get(f"manufacturer_{index}", values.get(f"manufacturer_name_{index}", "")),
                        values.get(f"manufacturer_item_number_{index}", values.get(f"manufacturer_part_number_{index}", "")))
                       for index in range(1, 100) if f"manufacturer_{index}" in values or f"manufacturer_name_{index}" in values
                       or f"manufacturer_item_number_{index}" in values or f"manufacturer_part_number_{index}" in values]
        populated = [(index, name, mpn) for index, name, mpn in pair_values if name or mpn]
        if not item:
            return {**base, "valid": False, "reason": "blank_item_number", "decisions": decisions}
        if quantity is None:
            parent = (row_number == header_index + 2 and category == "Printed Circuit Board Assembly"
                      and not refdes and not populated)
            if parent:
                decisions.append({"row": row_number, "item_number": item, "decision": "omitted_parent_blank_quantity"})
                continue
            return {**base, "valid": False, "reason": "blank_quantity", "row": row_number, "decisions": decisions}
        if math.isnan(quantity) or quantity < 0:
            return {**base, "valid": False, "reason": "invalid_quantity", "row": row_number, "decisions": decisions}
        if quantity == 0:
            if category in {"Procedure", "Drawing"}:
                decisions.append({"row": row_number, "item_number": item,
                                  "decision": f"omitted_{category.lower()}_zero_quantity"})
                continue
            return {**base, "valid": False, "reason": "zero_quantity", "row": row_number, "decisions": decisions}
        source_items.append({"item_number": item, "quantity": quantity, "pairs": populated})
        decisions.append({"row": row_number, "item_number": item, "decision": "include"})
        for index, name, mpn in populated:
            if index > 8 and name and mpn:
                decisions.append({"row": row_number, "item_number": item, "pair": index, "decision": "omitted_by_contract"})
            elif index > 8:
                return {**base, "valid": False, "reason": "half_populated_manufacturer_pair", "row": row_number, "pair": index, "decisions": decisions}

    frame = pd.read_excel(io.BytesIO(output), engine="openpyxl", dtype=object)
    out_headers = {str(name): index for index, name in enumerate(frame.columns)}
    required = set(CANONICAL_COLUMNS)
    if set(frame.columns) != required:
        return {**base, "valid": False, "reason": "output_schema", "decisions": decisions}
    output_items = {}
    for row_index, row in frame.iterrows():
        item = _text(row.get("item_number"))
        if not item or item in output_items:
            return {**base, "valid": False, "reason": "duplicate_output_identity", "decisions": decisions}
        output_items[item] = row
    source_ids = {item["item_number"] for item in source_items}
    if len(source_ids) != len(source_items):
        return {**base, "valid": False, "reason": "duplicate_source_identity", "decisions": decisions}
    output_ids = set(output_items)
    if not source_ids <= output_ids or output_ids - source_ids:
        return {**base, "valid": False, "reason": "identity_coverage", "decisions": decisions}
    for item in source_items:
        row = output_items[item["item_number"]]
        for index, name, mpn in item["pairs"]:
            if index > 8:
                continue
            out_name, out_mpn = _text(row.get(f"manufacturer_{index}")), _text(row.get(f"manufacturer_item_number_{index}"))
            if out_name.casefold() != name.casefold() or out_mpn != mpn:
                return {**base, "valid": False, "reason": "manufacturer_pair_mismatch", "item_number": item["item_number"], "pair": index, "decisions": decisions}
    omissions = [{key: decision[key] for key in ("row", "item_number", "decision") if key in decision}
                 for decision in decisions if str(decision.get("decision", "")).startswith("omitted_")]
    evidence = {**base, "valid": True, "reason": "valid", "source_rows": len(source_items),
                "output_rows": len(frame), "included_count": len(source_items),
                "omitted_count": len(omissions), "omissions": omissions, "decisions": decisions}
    evidence["decision_hash"] = _digest(decisions)
    return evidence


def is_arena_source(source: bytes) -> bool:
    """Return true only for the exact Arena header family."""
    try:
        rows = html_table_raw_rows(source)
        index, headers = _headers(rows)
        return index >= 0 and {"item_number", "item_name", "item_category", "quantity"} <= headers.keys()
    except Exception:
        return False