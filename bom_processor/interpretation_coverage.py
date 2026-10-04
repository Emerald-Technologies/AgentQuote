"""Offline source/output coverage verification, not semantic acceptance.

Input pinned source/XLSX bytes and an untrusted JSON sidecar. Output bounded
verification facts. No model, storage, job or approval calls. Run focused pytest.
"""
from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from typing import Any

from .core import CANONICAL_COLUMNS
from .interpretation_inventory import EXTRACTION_VERSION, MAX_CELLS, extract_inventory

VERSION = "bom-interpretation.v1"
MAX_SIDECAR_BYTES = 1024 * 1024
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_ROLES = {"component", "alternate", "document", "structural", "unresolved"}


class CoverageError(ValueError):
    """Stable failure; never include raw source/sidecar values."""


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise CoverageError(code)


def _keys(value: Any, keys: set[str]) -> None:
    _require(isinstance(value, dict) and set(value) == keys, "SCHEMA_KEYS_INVALID")


def _identifier(value: Any) -> None:
    _require(isinstance(value, str) and bool(_ID.fullmatch(value)), "IDENTIFIER_INVALID")


def _locator(value: Any) -> tuple[str, int, int]:
    _keys(value, {"sheet", "row", "column"})
    _require(isinstance(value["sheet"], str) and 0 < len(value["sheet"]) <= 128
             and type(value["row"]) is int and 1 <= value["row"] <= 100_000
             and type(value["column"]) is int and 1 <= value["column"] <= 512, "LOCATOR_INVALID")
    return value["sheet"], value["row"], value["column"]


def _text(value: Any) -> str:
    return str(value).strip()


def _same(left: Any, right: Any, numeric: bool) -> bool:
    if not numeric:
        return _text(left) == _text(right)
    if isinstance(left, bool) or isinstance(right, bool):
        return False
    try:
        a, b = Decimal(str(left)), Decimal(str(right))
        return a.is_finite() and b.is_finite() and a == b
    except (InvalidOperation, ValueError):
        return False


def _pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        _require(key not in result, "DUPLICATE_JSON_KEY")
        result[key] = value
    return result


def verify_coverage(source: bytes, extension: str, output: bytes, sidecar: bytes) -> dict[str, Any]:
    """Verify direct source values only; relationships remain agent proposals."""
    _require(isinstance(sidecar, bytes) and 0 < len(sidecar) <= MAX_SIDECAR_BYTES, "SIDECAR_SIZE_LIMIT")
    try:
        proposal = json.loads(sidecar.decode("utf-8"), object_pairs_hook=_pairs_hook,
                              parse_constant=lambda _: (_ for _ in ()).throw(CoverageError("JSON_NUMBER_INVALID")))
    except (UnicodeError, ValueError, RecursionError) as exc:
        if isinstance(exc, CoverageError):
            raise
        raise CoverageError("SIDECAR_JSON_INVALID") from exc
    _keys(proposal, {"version", "source", "output", "coverage", "records", "findings"})
    _require(proposal["version"] == VERSION, "SCHEMA_VERSION_INVALID")
    _keys(proposal["source"], {"sha256", "size_bytes", "extraction_version"})
    _keys(proposal["output"], {"sha256", "size_bytes", "sheet"})
    for binding in (proposal["source"], proposal["output"]):
        _require(isinstance(binding["sha256"], str) and bool(re.fullmatch(r"[0-9a-f]{64}", binding["sha256"]))
                 and type(binding["size_bytes"]) is int and binding["size_bytes"] > 0, "BINDING_TYPE_INVALID")
    _require(isinstance(proposal["source"]["extraction_version"], str)
             and isinstance(proposal["output"]["sheet"], str), "BINDING_TYPE_INVALID")
    original = extract_inventory(source, extension)
    parsed = extract_inventory(output, ".xlsx")
    _require(proposal["source"] == {"sha256": original.sha256, "size_bytes": original.size_bytes,
                                    "extraction_version": EXTRACTION_VERSION}, "SOURCE_BINDING_INVALID")
    _require(proposal["output"]["sha256"] == sha256(output).hexdigest()
             and type(proposal["output"]["size_bytes"]) is int
             and proposal["output"]["size_bytes"] == len(output), "OUTPUT_BINDING_INVALID")
    sheet = proposal["output"]["sheet"]
    _require(parsed.sheets == (sheet,), "OUTPUT_SHEETS_INVALID")
    _require(tuple(parsed.cells.get((sheet, 1, col)) for col in range(1, 26)) == tuple(CANONICAL_COLUMNS),
             "OUTPUT_HEADERS_INVALID")
    _require(all(column <= 25 for _, _, column in parsed.cells), "OUTPUT_COLUMNS_INVALID")
    records, coverage, findings = proposal["records"], proposal["coverage"], proposal["findings"]
    _require(isinstance(records, list) and 0 < len(records) <= 100_000
             and isinstance(coverage, list) and len(coverage) <= MAX_CELLS
             and isinstance(findings, list) and len(findings) <= 1000, "SCHEMA_COLLECTION_LIMIT")
    record_ids: set[str] = set()
    output_rows: set[int] = set()
    links: list[tuple[tuple[str, int, int], str, str]] = []
    for record in records:
        _keys(record, {"id", "output_row", "category", "fields"})
        _identifier(record["id"])
        row = record["output_row"]
        _require(record["id"] not in record_ids and type(row) is int and 2 <= row <= 100_001
                 and row not in output_rows and isinstance(record["category"], str)
                 and record["category"] in {"Part", "Document"}, "RECORD_INVALID")
        record_ids.add(record["id"]); output_rows.add(row)
        line = parsed.cells.get((sheet, row, 1))
        _require(type(line) is int and line == row - 1, "OUTPUT_LINE_NUMBER_INVALID")
        _require(parsed.cells.get((sheet, row, 5)) == record["category"], "OUTPUT_CATEGORY_INVALID")
        fields = record["fields"]
        _require(isinstance(fields, dict) and set(fields) <= set(CANONICAL_COLUMNS) - {"line_number", "item_category"},
                 "FIELD_KEYS_INVALID")
        for required in ("item_number", "item_name"):
            _require(bool(parsed.cells.get((sheet, row, CANONICAL_COLUMNS.index(required) + 1))), "OUTPUT_IDENTITY_MISSING")
        quantity = parsed.cells.get((sheet, row, 7))
        if record["category"] == "Part":
            _require(quantity is not None and _same(quantity, quantity, True), "PART_QUANTITY_INVALID")
        populated = {field for col, field in enumerate(CANONICAL_COLUMNS, 1)
                     if col not in (1, 5) and (sheet, row, col) in parsed.cells}
        _require(set(fields) == populated, "OUTPUT_FIELD_COVERAGE_INVALID")
        for field, ref in fields.items():
            location = _locator(ref)
            _require(location in original.cells, "SOURCE_CELL_MISSING")
            target = parsed.cells[(sheet, row, CANONICAL_COLUMNS.index(field) + 1)]
            _require(_same(original.cells[location], target, field == "quantity"), "SOURCE_VALUE_CHANGED")
            links.append((location, record["id"], field))
        for slot in range(1, 9):
            manufacturer = f"manufacturer_{slot}"
            mpn = f"manufacturer_item_number_{slot}"
            _require((manufacturer in fields) == (mpn in fields), "MANUFACTURER_PAIR_INCOMPLETE")
            if manufacturer in fields:
                _require(_locator(fields[manufacturer])[:2] == _locator(fields[mpn])[:2], "PAIR_RELATION_UNRESOLVED")
    _require({row for _, row, _ in parsed.cells if row > 1} == output_rows, "OUTPUT_RECORD_COVERAGE_INVALID")
    decisions: dict[tuple[str, int, int], dict[str, Any]] = {}
    unresolved = False
    for unit in coverage:
        _keys(unit, {"cell", "disposition", "record_ids", "rationale"})
        location = _locator(unit["cell"])
        _require(location in original.cells and location not in decisions, "SOURCE_COVERAGE_DUPLICATE_OR_UNKNOWN")
        _require(isinstance(unit["disposition"], str) and unit["disposition"] in _ROLES
                 and isinstance(unit["rationale"], str) and 0 < len(unit["rationale"]) <= 1000,
                 "DISPOSITION_INVALID")
        ids = unit["record_ids"]
        _require(isinstance(ids, list) and len(ids) <= 100 and all(isinstance(x, str) for x in ids)
                 and len(set(ids)) == len(ids) and set(ids) <= record_ids, "DISPOSITION_RECORD_INVALID")
        _require(bool(ids) if unit["disposition"] in {"component", "alternate", "document"} else not ids,
                 "DISPOSITION_RECORD_INVALID")
        unresolved |= unit["disposition"] == "unresolved"
        decisions[location] = unit
    _require(set(decisions) == set(original.cells), "SOURCE_COVERAGE_INCOMPLETE")
    quantity_links: set[tuple[str, int, int]] = set()
    for location, record_id, field in links:
        _require(record_id in decisions[location]["record_ids"], "LINK_DISPOSITION_MISMATCH")
        if field == "quantity":
            _require(location not in quantity_links, "DUPLICATE_QUANTITY_CONTRIBUTION")
            quantity_links.add(location)
    linked_cells = {location for location, _, _ in links}
    # Coverage labels alone do not demonstrate preservation. Unlinked content
    # assigned a semantic role remains a blocking gap, not a clean verification.
    unrepresented = [location for location, unit in decisions.items()
                     if unit["disposition"] in {"component", "alternate", "document"}
                     and location not in linked_cells]
    blocking = unresolved or bool(unrepresented)
    for finding in findings:
        _keys(finding, {"code", "severity", "rationale", "cells"})
        _identifier(finding["code"])
        _require(isinstance(finding["severity"], str) and finding["severity"] in {"information", "warning", "blocking"}
                 and isinstance(finding["rationale"], str) and 0 < len(finding["rationale"]) <= 1000
                 and isinstance(finding["cells"], list) and len(finding["cells"]) <= 1000, "FINDING_INVALID")
        for ref in finding["cells"]:
            _require(_locator(ref) in original.cells, "FINDING_CELL_INVALID")
        blocking |= finding["severity"] == "blocking"
    return {"version": "bom-coverage-verification.v1", "source_sha256": original.sha256,
            "output_sha256": parsed.sha256, "sidecar_sha256": sha256(sidecar).hexdigest(),
            "source_cells": len(original.cells), "output_records": len(records),
            "provenance_verified": True, "has_blocking_findings": blocking,
            "unrepresented_semantic_cells": len(unrepresented),
            "semantic_acceptance": "not_evaluated", "automatic_acceptance_authorized": False}