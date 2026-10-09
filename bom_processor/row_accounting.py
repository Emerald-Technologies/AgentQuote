"""Row accounting for one parsed BOM (plan 8.8 step 4a, Nick 2026-10-09).

Inputs: pinned source bytes and extension, the parser's canonical XLSX output, and the row map the
parser wrote with --row-map (version bom-row-map.v1). Output: counts and failing source-row
locations only, never customer values. Code checks, on every row of the real file:
- every non-empty source row belongs to exactly one output line or is skipped with a reason;
- every output line is mapped, and every populated output cell (except line_number and
  item_category) cites a source cell in its own line holding the same value, or one separated part
  of it, or a stated derivation for unit_of_measure / bom_notes only;
- every row in a line contributes at least one cited value; both halves of a pair cite one row;
- a skipped row holding a part number and a positive quantity in mapped columns is a failure;
- every source column with data in BOM lines feeds a field or is declared ignored.
Run: python -m pytest tests/test_row_accounting.py
"""
from __future__ import annotations

import csv
import io
import json
import re
from collections import Counter
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from typing import Any

from .canonical_format import canonical_columns, canonical_pair_count
from .interpretation_inventory import MAX_BYTES, MAX_CELLS, MAX_COLUMNS, MAX_ROWS, SourceInventory, extract_inventory

VERSION = "bom-row-map.v1"
RESULT_VERSION = "bom-row-accounting.v1"
ROW_MAP_NAME = "row-map.json"
MAX_ROW_MAP_BYTES = 8 * 1024 * 1024
MAX_RECORDED_ROWS = 500
MAX_RECORDED_COLUMNS = 200
MAX_SKIPPED_ROW_SPAN = 200_000
SKIP_REASONS = frozenset({"header", "title", "blank", "total", "note", "section", "signature",
                          "other_sheet", "other"})
DERIVABLE_FIELDS = frozenset({"unit_of_measure", "bom_notes"})
_FIXED = frozenset({"line_number", "item_category"})
# Sheet names the parser cites for sources without named sheets.
CSV_SHEET = "CSV"
HTML_SHEET = "HTML_TABLE_1"
# Separators a combined cell may use; " / " only with spaces so part numbers with "/" stay whole.
_PARTS = re.compile(r"\s*(?:[;,|\n]|\s/\s)\s*")


class RowMapError(ValueError):
    """Stable structural failure; never includes customer values."""


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise RowMapError(code)


def _keys(value: Any, keys: set[str], code: str) -> None:
    _require(isinstance(value, dict) and set(value) == keys, code)


def _hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        _require(key not in result, "ROW_MAP_DUPLICATE_KEY")
        result[key] = value
    return result


def _reject_constant(_: str) -> Any:
    raise RowMapError("ROW_MAP_JSON_INVALID")


def _text(value: Any) -> str:
    return str(value).strip()


def _number(value: Any) -> Decimal | None:
    if isinstance(value, bool):
        return None
    try:
        number = Decimal(_text(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _same(source: Any, target: Any, numeric: bool) -> bool:
    if numeric:
        a, b = _number(source), _number(target)
        return a is not None and b is not None and a == b
    return _text(source) == _text(target)


def _part(source: Any, target: Any) -> bool:
    wanted = _text(target)
    return bool(wanted) and wanted in {part for part in _PARTS.split(_text(source)) if part}


def _name(value: Any) -> bool:
    return isinstance(value, str) and 0 < len(value) <= 128


def _reason(value: Any) -> bool:
    return isinstance(value, str) and 0 < len(value.strip()) and len(value) <= 500


def source_inventory(source: bytes, extension: str) -> SourceInventory:
    """Inventory of the source; CSV is read here (one sheet named CSV) with the inventory's bounds."""
    if extension.casefold() != ".csv":
        return extract_inventory(source, extension)
    _require(isinstance(source, bytes) and 0 < len(source) <= MAX_BYTES, "SOURCE_SIZE_LIMIT")
    try:
        text = source.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise RowMapError("SOURCE_CSV_ENCODING") from None
    cells: dict[tuple[str, int, int], Any] = {}
    try:
        for row, values in enumerate(csv.reader(io.StringIO(text, newline="")), 1):
            _require(row <= MAX_ROWS and len(values) <= MAX_COLUMNS, "SOURCE_DIMENSION_LIMIT")
            for column, value in enumerate(values, 1):
                if value != "":
                    _require(len(cells) < MAX_CELLS, "SOURCE_CELL_LIMIT")
                    cells[(CSV_SHEET, row, column)] = value
    except csv.Error:
        raise RowMapError("SOURCE_CSV_INVALID") from None
    return SourceInventory(sha256(source).hexdigest(), len(source), (CSV_SHEET,), cells)


def _load(row_map: bytes) -> dict[str, Any]:
    _require(isinstance(row_map, bytes) and 0 < len(row_map) <= MAX_ROW_MAP_BYTES, "ROW_MAP_SIZE_LIMIT")
    try:
        proposal = json.loads(row_map.decode("utf-8"), object_pairs_hook=_hook,
                              parse_constant=_reject_constant)
    except RowMapError:
        raise
    except (UnicodeError, ValueError, RecursionError):
        raise RowMapError("ROW_MAP_JSON_INVALID") from None
    _keys(proposal, {"version", "lines", "skipped", "ignored_columns"}, "ROW_MAP_KEYS_INVALID")
    _require(proposal["version"] == VERSION, "ROW_MAP_VERSION_INVALID")
    return proposal


def verify_row_map(source: bytes, extension: str, output: bytes, row_map: bytes) -> dict[str, Any]:
    """Account for every source row of one parsed BOM. Raises RowMapError on a malformed map."""
    proposal = _load(row_map)
    original = source_inventory(source, extension)
    parsed = extract_inventory(output, ".xlsx")
    _require(len(parsed.sheets) == 1, "OUTPUT_SHEETS_INVALID")
    _require(not parsed.formula_cells, "OUTPUT_FORMULA_PRESENT")
    out_sheet = parsed.sheets[0]
    width = max([column for _, row, column in parsed.cells if row == 1] or [0])
    pairs = canonical_pair_count(parsed.cells.get((out_sheet, 1, column)) for column in range(1, width + 1))
    _require(pairs is not None, "OUTPUT_HEADERS_INVALID")
    columns = canonical_columns(pairs)
    _require(all(column <= len(columns) for _, _, column in parsed.cells), "OUTPUT_COLUMNS_INVALID")

    # Non-empty source rows (whitespace-only cells count as empty) and their populated columns.
    source_rows: dict[tuple[str, int], set[int]] = {}
    for (sheet, row, column), value in original.cells.items():
        if _text(value):
            source_rows.setdefault((sheet, row), set()).add(column)
    output_rows = {row for _, row, _ in parsed.cells if row > 1}
    output_fields: dict[int, set[str]] = {}
    for _, row, column in parsed.cells:
        if row > 1 and columns[column - 1] not in _FIXED:
            output_fields.setdefault(row, set()).add(columns[column - 1])

    failed: dict[tuple[str, int], str] = {}

    def fail(key: tuple[str, int], code: str) -> None:
        failed.setdefault(key, code)

    lines = proposal["lines"]
    _require(isinstance(lines, list) and 0 < len(lines) <= 100_000, "ROW_MAP_LINES_INVALID")
    owner: dict[tuple[str, int], int] = {}
    mapped_output: set[int] = set()
    used_columns: set[tuple[str, int]] = set()
    identity_columns: set[tuple[str, int]] = set()
    quantity_columns: set[tuple[str, int]] = set()
    allowed_fields = set(columns) - _FIXED
    for line in lines:
        _keys(line, {"output_row", "sheet", "rows", "fields"}, "ROW_MAP_LINE_INVALID")
        out, sheet, rows, fields = line["output_row"], line["sheet"], line["rows"], line["fields"]
        _require(type(out) is int and out in output_rows and out not in mapped_output, "ROW_MAP_OUTPUT_ROW_INVALID")
        _require(_name(sheet) and sheet in original.sheets, "ROW_MAP_SHEET_INVALID")
        _require(isinstance(rows, list) and 0 < len(rows) <= 1000 and len(set(map(repr, rows))) == len(rows)
                 and all(type(row) is int and 1 <= row <= 100_000 for row in rows), "ROW_MAP_ROWS_INVALID")
        _require(isinstance(fields, dict) and set(fields) <= allowed_fields, "ROW_MAP_FIELDS_INVALID")
        mapped_output.add(out)
        first = (sheet, min(rows))
        for row in rows:
            key = (sheet, row)
            if key not in source_rows:
                fail(key, "ROW_EMPTY_OR_MISSING")
            elif key in owner:
                fail(key, "ROW_IN_TWO_LINES")
            else:
                owner[key] = out
        if output_fields.get(out, set()) - set(fields):
            fail(first, "FIELD_NOT_LINKED")
        contributed: set[int] = set()
        for field, link in fields.items():
            target = parsed.cells.get((out_sheet, out, columns.index(field) + 1))
            if isinstance(link, dict):
                _keys(link, {"derived"}, "ROW_MAP_LINK_INVALID")
                _require(_reason(link["derived"]), "ROW_MAP_LINK_INVALID")
                if field not in DERIVABLE_FIELDS:
                    fail(first, "FIELD_DERIVATION_NOT_ALLOWED")
                continue
            _require(isinstance(link, list) and len(link) in (2, 3)
                     and all(type(item) is int and item >= 1 for item in link[:2])
                     and (len(link) == 2 or link[2] == "part"), "ROW_MAP_LINK_INVALID")
            row, column = link[0], link[1]
            if row not in rows:
                fail(first, "FIELD_OUTSIDE_LINE")
                continue
            value = original.cells.get((sheet, row, column))
            matches = (value is not None and target is not None
                       and (_part(value, target) if len(link) == 3 else _same(value, target, field == "quantity")))
            if not matches:
                fail((sheet, row), "FIELD_VALUE_MISMATCH")
                continue
            contributed.add(row)
            used_columns.add((sheet, column))
            if field == "item_number" or field.startswith("manufacturer_item_number_"):
                identity_columns.add((sheet, column))
            if field == "quantity":
                quantity_columns.add((sheet, column))
        for row in rows:
            if (sheet, row) in source_rows and row not in contributed:
                fail((sheet, row), "ROW_CONTRIBUTES_NOTHING")
        for index in range(1, pairs + 1):
            manufacturer = fields.get(f"manufacturer_{index}")
            mpn = fields.get(f"manufacturer_item_number_{index}")
            if isinstance(manufacturer, list) and isinstance(mpn, list) and manufacturer[0] != mpn[0]:
                fail((sheet, mpn[0]), "PAIR_ROWS_DIFFER")
    _require(mapped_output == output_rows, "ROW_MAP_OUTPUT_COVERAGE_INVALID")

    skipped = proposal["skipped"]
    _require(isinstance(skipped, list) and len(skipped) <= 10_000, "ROW_MAP_SKIP_INVALID")
    skipped_rows: dict[tuple[str, int], str] = {}
    span = 0
    for entry in skipped:
        _keys(entry, {"sheet", "rows", "reason", "rationale"}, "ROW_MAP_SKIP_INVALID")
        ranges = entry["rows"]
        _require(_name(entry["sheet"]) and entry["sheet"] in original.sheets
                 and entry["reason"] in SKIP_REASONS and _reason(entry["rationale"])
                 and isinstance(ranges, list) and 0 < len(ranges) <= 10_000, "ROW_MAP_SKIP_INVALID")
        for item in ranges:
            _require(isinstance(item, list) and len(item) == 2 and all(type(n) is int for n in item)
                     and 1 <= item[0] <= item[1] <= 100_000, "ROW_MAP_SKIP_INVALID")
            span += item[1] - item[0] + 1
            _require(span <= MAX_SKIPPED_ROW_SPAN, "ROW_MAP_SKIP_INVALID")
            for row in range(item[0], item[1] + 1):
                key = (entry["sheet"], row)
                if key not in source_rows:
                    continue  # Ranges may cover blank rows.
                if key in owner:
                    fail(key, "ROW_IN_LINE_AND_SKIPPED")
                elif key in skipped_rows:
                    fail(key, "ROW_SKIPPED_TWICE")
                else:
                    skipped_rows[key] = entry["reason"]
    for key in source_rows:
        if key not in owner and key not in skipped_rows:
            fail(key, "ROW_UNACCOUNTED")
    for key, reason in skipped_rows.items():
        if reason == "header":
            continue
        sheet, row = key
        has_identity = any((sheet, column) in identity_columns for column in source_rows[key])
        has_quantity = any((sheet, column) in quantity_columns
                           and (_number(original.cells[(sheet, row, column)]) or 0) > 0
                           for column in source_rows[key])
        if has_identity and has_quantity:
            fail(key, "SKIPPED_ROW_LOOKS_LIKE_LINE")

    ignored = proposal["ignored_columns"]
    _require(isinstance(ignored, list) and len(ignored) <= 5_000, "ROW_MAP_IGNORED_INVALID")
    ignored_columns: set[tuple[str, int]] = set()
    for entry in ignored:
        _keys(entry, {"sheet", "column", "reason"}, "ROW_MAP_IGNORED_INVALID")
        _require(_name(entry["sheet"]) and entry["sheet"] in original.sheets and type(entry["column"]) is int
                 and 1 <= entry["column"] <= 512 and _reason(entry["reason"])
                 and (entry["sheet"], entry["column"]) not in ignored_columns, "ROW_MAP_IGNORED_INVALID")
        ignored_columns.add((entry["sheet"], entry["column"]))
    line_columns = {(sheet, column) for (sheet, row), cols in source_rows.items()
                    if (sheet, row) in owner for column in cols}
    unmapped = sorted(line_columns - used_columns - ignored_columns)
    contradicted = sorted(ignored_columns & used_columns)

    recorded = sorted(failed.items())
    return {
        "version": RESULT_VERSION,
        "valid": not failed and not unmapped and not contradicted,
        "source_sha256": original.sha256,
        "output_sha256": parsed.sha256,
        "row_map_sha256": sha256(row_map).hexdigest(),
        "source_rows": len(source_rows),
        "line_rows": len(owner),
        "skipped_rows": len(skipped_rows),
        "output_lines": len(mapped_output),
        "failed_row_count": len(failed),
        "failed_rows": [{"sheet": sheet, "row": row, "code": code}
                        for (sheet, row), code in recorded[:MAX_RECORDED_ROWS]],
        "failure_codes": dict(sorted(Counter(code for code in failed.values()).items())),
        "unmapped_columns": [{"sheet": sheet, "column": column}
                             for sheet, column in unmapped[:MAX_RECORDED_COLUMNS]],
        "ignored_but_used_columns": [{"sheet": sheet, "column": column}
                                     for sheet, column in contradicted[:MAX_RECORDED_COLUMNS]],
    }
