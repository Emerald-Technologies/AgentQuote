"""Row accounting for one parsed BOM (plan 8.8 step 4a, Nick 2026-10-09).

Inputs: pinned source bytes and extension, the parser's canonical XLSX output, and the row map the
parser wrote with --row-map (bom-row-map.v1 or v2). Output: counts and failing source-row
locations only, never customer values. Code checks, on every row of the real file:
- every non-empty source row belongs to exactly one output line or is skipped with a reason;
- every output line is mapped, and every populated output cell (except line_number and
  item_category) cites a source cell in its own line holding the same value, or one separated part
  of it, or a stated derivation for unit_of_measure / bom_notes only;
- a manufacturer may cite [row, column, "code"]: the source cell is a short customer code, a space and
  exactly the written name ("63 ACME INC." -> "ACME INC."); the raw cell stays cited (PLM readiness
  D1, Nick 2026-10-12). Names written with such a code still in front are counted, never failed;
- every row in a line contributes at least one cited value; both halves of a pair cite one row;
- a skipped row holding a part number and a positive quantity in mapped columns is a failure;
- a skipped row shaped like an alternate (a value in a manufacturer or MPN column and nothing in any
  other mapped column) is a failure: dropped as a note it loses a pair (PLM readiness D3, Nick
  2026-10-12);
- row map v2 (sub-assembly plan unit 2, Nick 2026-10-12): a line may state its parent line, the cell
  that shows it and how (a level column, dotted numbers, a parent part number column or indentation);
  code works out every parent from the cited cells and fails a different claim. All lines or none,
  one kind and one column. Quantities stay as printed (per parent); roll-down happens after this
  check, never in the parser. v1 maps (no parents) are still accepted;
- a value kept on another sheet keyed by part number (unit 2b, AMAT shape: MPNs on an MFG sheet) is
  cited as {"lookup": [sheet, row, column], "key": key column}: the key cell must equal the line's part
  number, the cell must hold the value, every row on that sheet holding a maker or MPN for the part
  must be carried by the line (LOOKUP_ROW_NOT_CARRIED), and looked-up rows count as accounted for;
- every source column with data in BOM lines feeds a field or is declared ignored;
- a quantity may be cited as {"derived": reason} only for an assembly's one bare board when the BOM
  prints none (QQ A052, Nick 2026-10-14): a Part of quantity 1 with a bom_notes flag, no designator or
  manufacturer/MPN, an empty quantity cell, and at most one such line per parent line;
- the manufacturer/MPN pattern holds on every line row (plan 8.12, step 4d): a column is never a
  manufacturer in one pair and an MPN in another, a manufacturer column always pairs with the same
  MPN column, a populated pair cell in a mapped pair column is carried, a combined cell splits into
  equal name and MPN counts and every part is carried, and no ignored column holds known makers.
Run: python -m pytest tests/test_row_accounting.py tests/test_manufacturer_layouts.py
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

VERSION_V1 = "bom-row-map.v1"
# v2 (sub-assembly plan unit 2, Nick 2026-10-12): a line may state its parent line.
VERSION = "bom-row-map.v2"
VERSIONS = frozenset({VERSION_V1, VERSION})
RESULT_VERSION = "bom-row-accounting.v1"
ROW_MAP_NAME = "row-map.json"
MAX_ROW_MAP_BYTES = 8 * 1024 * 1024
MAX_RECORDED_ROWS = 500
MAX_RECORDED_MISMATCHES = 200
MAX_RECORDED_COLUMNS = 200
MAX_SKIPPED_ROW_SPAN = 200_000
SKIP_REASONS = frozenset({"header", "title", "blank", "total", "note", "section", "signature",
                          "other_sheet", "other"})
DERIVABLE_FIELDS = frozenset({"unit_of_measure", "bom_notes"})
# QQ A052 (Nick 2026-10-14): an assembly's one bare board may be given quantity 1 when the BOM prints
# none, flagged in bom_notes for a person. Only that: a Part line of quantity exactly 1 with a note, no
# designator and no manufacturer/MPN (so a real placed part with a blank quantity still stops), no
# value in its rows' quantity column, and at most one such line per assembly group (parent line).
BOARD_QUANTITY_INVALID = "BOARD_QUANTITY_DERIVATION_INVALID"
BOARD_QUANTITY_PRINTED = "BOARD_QUANTITY_PRINTED"
BOARD_QUANTITY_REPEATED = "BOARD_QUANTITY_REPEATED"
_FIXED = frozenset({"line_number", "item_category"})
# Sheet names the parser cites for sources without named sheets.
CSV_SHEET = "CSV"
HTML_SHEET = "HTML_TABLE_1"
# Separators a combined cell may use; " / " only with spaces so part numbers with "/" stay whole.
_PARTS = re.compile(r"\s*(?:[;,|\n]|\s/\s)\s*")
# A customer's own maker code in front of the name: 1-3 digits and a space ("3M" has no space).
_MAKER_CODE = re.compile(r"\d{1,3}\s+(\S.*)", re.DOTALL)
LINK_KINDS = frozenset({"part", "code"})


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


def _code(source: Any, target: Any) -> bool:
    """The source is a maker code, a space and exactly the written name."""
    match = _MAKER_CODE.fullmatch(_text(source))
    return match is not None and match.group(1).strip() == _text(target)


def _is_maker(field: str) -> bool:
    return field.startswith("manufacturer_") and not field.startswith("manufacturer_item_number_")


# How a BOM shows sub-assemblies; the parser names one per file and the cell it read it from.
STRUCTURE_KINDS = frozenset({"level", "number", "parent", "indent"})
_DOTTED = re.compile(r"\d+(?:\.\d+)*")


def _structure_key(kind: str, value: Any) -> Any:
    """The comparable structure value of a cited cell, or None when the cell cannot show it."""
    if kind == "level":
        number = _number(value) if value is not None else None
        return int(number) if number is not None and number == number.to_integral_value() and number >= 0 else None
    if kind == "number":
        text = _text(value) if value is not None else ""
        return tuple(int(part) for part in text.split(".")) if _DOTTED.fullmatch(text) else None
    if kind == "indent":
        if not isinstance(value, str) or not value.strip():
            return None
        return len(value) - len(value.lstrip(" \t 　"))
    return _text(value).casefold() if value is not None and _text(value) else ""  # parent: "" is top


def _expected_parents(kind: str, ordered: list[tuple[int, Any, Any]]) -> dict[int, int | None]:
    """Parent output row of each line, from (output_row, key, item_number) in source order.

    level / indent: the nearest earlier line with a smaller value. number: the nearest earlier line
    whose number is this one without its last part. parent: the nearest earlier line whose part
    number is the cited parent; none (the quoted assembly itself, or no parent) is top level.
    """
    result: dict[int, int | None] = {}
    for index, (out, key, _) in enumerate(ordered):
        earlier = reversed(ordered[:index])
        if kind in {"level", "indent"}:
            found = next((o for o, k, _ in earlier if k < key), None)
        elif kind == "number":
            found = next((o for o, k, _ in earlier if k == key[:-1]), None) if len(key) > 1 else None
        else:
            found = next((o for o, _, item in earlier if key and item == key), None)
        result[out] = found
    return result


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
    _require(proposal["version"] in VERSIONS, "ROW_MAP_VERSION_INVALID")
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
    # Which output field cited which source column on a mismatched row (round 4, A037): locations
    # and field names only, so the refinement can say what to correct without customer values.
    mismatches: list[dict[str, Any]] = []
    # Output fields written without a link, per line (round 11, A043: the rewrite saw only the row).
    unlinked: list[dict[str, Any]] = []

    def fail(key: tuple[str, int], code: str) -> None:
        failed.setdefault(key, code)

    lines = proposal["lines"]
    _require(isinstance(lines, list) and 0 < len(lines) <= 100_000, "ROW_MAP_LINES_INVALID")
    owner: dict[tuple[str, int], int] = {}
    mapped_output: set[int] = set()
    used_columns: set[tuple[str, int]] = set()
    identity_columns: set[tuple[str, int]] = set()
    quantity_columns: set[tuple[str, int]] = set()
    # Manufacturer/MPN pattern facts (plan 8.12, step 4d).
    column_roles: dict[tuple[str, int], set[str]] = {}
    pair_columns: dict[tuple[str, int], set[int]] = {}
    cited_cells: set[tuple[str, int, int]] = set()
    pair_cells: set[tuple[str, int, int]] = set()
    part_links: Counter[tuple[str, int, int]] = Counter()
    pair_rows: list[tuple[str, int, int, int, bool]] = []
    maker_codes_kept = 0
    # Unit 2b: rows on other sheets cited by lookup, the (sheet, key column) pairs used, and each
    # line's looked-up rows. A looked-up row may serve several lines (a part used in several places).
    looked_up: dict[tuple[str, int], set[int]] = {}
    lookup_keys: set[tuple[str, int]] = set()
    line_lookups: dict[int, set[tuple[str, int]]] = {}
    item_column = columns.index("item_number") + 1
    allowed_fields = set(columns) - _FIXED
    claims: list[tuple[int, str, int, dict[str, Any]]] = []
    # A052: lines whose quantity is cited as derived (the bare board), checked after every line.
    derived_quantities: list[tuple[int, str, list[int], tuple[str, int], Any]] = []
    for line in lines:
        base = {"output_row", "sheet", "rows", "fields"}
        _keys(line, base | ({"parent"} if proposal["version"] == VERSION and "parent" in line else set()),
              "ROW_MAP_LINE_INVALID")
        out, sheet, rows, fields = line["output_row"], line["sheet"], line["rows"], line["fields"]
        _require(type(out) is int and out in output_rows and out not in mapped_output, "ROW_MAP_OUTPUT_ROW_INVALID")
        _require(_name(sheet) and sheet in original.sheets, "ROW_MAP_SHEET_INVALID")
        _require(isinstance(rows, list) and 0 < len(rows) <= 1000 and len(set(map(repr, rows))) == len(rows)
                 and all(type(row) is int and 1 <= row <= 100_000 for row in rows), "ROW_MAP_ROWS_INVALID")
        _require(isinstance(fields, dict) and set(fields) <= allowed_fields, "ROW_MAP_FIELDS_INVALID")
        mapped_output.add(out)
        first = (sheet, min(rows))
        if "parent" in line:
            claim = line["parent"]
            _keys(claim, {"line", "from", "by"}, "ROW_MAP_PARENT_INVALID")
            _require(claim["by"] in STRUCTURE_KINDS
                     and (claim["line"] is None or (type(claim["line"]) is int and claim["line"] >= 2))
                     and isinstance(claim["from"], list) and len(claim["from"]) == 2
                     and all(type(item) is int and item >= 1 for item in claim["from"]), "ROW_MAP_PARENT_INVALID")
            claims.append((out, sheet, min(rows), claim))
            if claim["from"][0] in rows and (sheet, claim["from"][0], claim["from"][1]) in original.cells:
                used_columns.add((sheet, claim["from"][1]))
                cited_cells.add((sheet, claim["from"][0], claim["from"][1]))
        for row in rows:
            key = (sheet, row)
            if key not in source_rows:
                fail(key, "ROW_EMPTY_OR_MISSING")
            elif key in owner:
                fail(key, "ROW_IN_TWO_LINES")
            else:
                owner[key] = out
        missing = output_fields.get(out, set()) - set(fields)
        if missing:
            fail(first, "FIELD_NOT_LINKED")
            for field in sorted(missing):
                if len(unlinked) < MAX_RECORDED_MISMATCHES:
                    unlinked.append({"sheet": first[0], "row": first[1], "field": field})
        contributed: set[int] = set()
        for field, link in fields.items():
            target = parsed.cells.get((out_sheet, out, columns.index(field) + 1))
            if isinstance(link, dict) and "lookup" in link:
                # Unit 2b (AMAT shape, Nick 2026-10-12): a value kept on another sheet, keyed by
                # the line's part number (for example MPNs on an MFG sheet).
                _keys(link, {"lookup", "key"}, "ROW_MAP_LINK_INVALID")
                cited, key_column = link["lookup"], link["key"]
                _require(isinstance(cited, list) and len(cited) == 3 and _name(cited[0])
                         and cited[0] in original.sheets and cited[0] != sheet
                         and all(type(item) is int and item >= 1 for item in cited[1:])
                         and type(key_column) is int and key_column >= 1, "ROW_MAP_LINK_INVALID")
                if field in {"item_number", "quantity"}:
                    fail(first, "LOOKUP_NOT_ALLOWED")
                    continue
                look_sheet, look_row, look_column = cited
                key_value = original.cells.get((look_sheet, look_row, key_column))
                item = parsed.cells.get((out_sheet, out, item_column))
                if key_value is None or item is None or not _text(item) or _text(key_value) != _text(item):
                    fail(first, "LOOKUP_KEY_MISMATCH")
                    continue
                value = original.cells.get((look_sheet, look_row, look_column))
                if value is None or target is None or not _same(value, target, False):
                    fail((look_sheet, look_row), "FIELD_VALUE_MISMATCH")
                    if len(mismatches) < MAX_RECORDED_MISMATCHES:
                        mismatches.append({"sheet": look_sheet, "row": look_row, "field": field,
                                           "column": look_column, "part": False})
                    continue
                looked_up.setdefault((look_sheet, look_row), set()).add(out)
                lookup_keys.add((look_sheet, key_column))
                line_lookups.setdefault(out, set()).add((look_sheet, look_row))
                used_columns.update({(look_sheet, look_column), (look_sheet, key_column)})
                cited_cells.add((look_sheet, look_row, look_column))
                if field.startswith("manufacturer_"):
                    role = "mpn" if field.startswith("manufacturer_item_number_") else "manufacturer"
                    column_roles.setdefault((look_sheet, look_column), set()).add(role)
                    pair_cells.add((look_sheet, look_row, look_column))
                continue
            if isinstance(link, dict):
                _keys(link, {"derived"}, "ROW_MAP_LINK_INVALID")
                _require(_reason(link["derived"]), "ROW_MAP_LINK_INVALID")
                if field == "quantity":
                    parent = line.get("parent")
                    derived_quantities.append((out, sheet, rows, first,
                                               parent.get("line") if isinstance(parent, dict) else None))
                elif field not in DERIVABLE_FIELDS:
                    fail(first, "FIELD_DERIVATION_NOT_ALLOWED")
                continue
            _require(isinstance(link, list) and len(link) in (2, 3)
                     and all(type(item) is int and item >= 1 for item in link[:2])
                     and (len(link) == 2 or link[2] in LINK_KINDS), "ROW_MAP_LINK_INVALID")
            row, column = link[0], link[1]
            kind = link[2] if len(link) == 3 else None
            if kind == "code" and not _is_maker(field):
                fail(first, "CODE_LINK_NOT_ALLOWED")
                continue
            if row not in rows:
                fail(first, "FIELD_OUTSIDE_LINE")
                continue
            value = original.cells.get((sheet, row, column))
            if kind == "part":
                matches = _part(value, target)
            elif kind == "code":
                matches = _code(value, target)
            else:
                matches = _same(value, target, field == "quantity")
            matches = value is not None and target is not None and matches
            if not matches:
                fail((sheet, row), "FIELD_VALUE_MISMATCH")
                if len(mismatches) < MAX_RECORDED_MISMATCHES:
                    mismatches.append({"sheet": sheet, "row": row, "field": field, "column": column,
                                       "part": kind == "part"})
                continue
            if kind is None and _is_maker(field) and _MAKER_CODE.fullmatch(_text(target)):
                maker_codes_kept += 1
            contributed.add(row)
            used_columns.add((sheet, column))
            cited_cells.add((sheet, row, column))
            if field == "item_number" or field.startswith("manufacturer_item_number_"):
                identity_columns.add((sheet, column))
            if field == "quantity":
                quantity_columns.add((sheet, column))
            if field.startswith("manufacturer_"):
                role = "mpn" if field.startswith("manufacturer_item_number_") else "manufacturer"
                column_roles.setdefault((sheet, column), set()).add(role)
                pair_cells.add((sheet, row, column))
                if kind == "part":
                    part_links[(sheet, row, column)] += 1
        for row in rows:
            if (sheet, row) in source_rows and row not in contributed:
                fail((sheet, row), "ROW_CONTRIBUTES_NOTHING")
        for index in range(1, pairs + 1):
            manufacturer = fields.get(f"manufacturer_{index}")
            mpn = fields.get(f"manufacturer_item_number_{index}")
            looked = [link["lookup"][:2] for link in (manufacturer, mpn) if isinstance(link, dict) and "lookup" in link]
            if len(looked) == 2 and looked[0] != looked[1]:
                fail(tuple(looked[1]), "PAIR_ROWS_DIFFER")
            if isinstance(manufacturer, list) and isinstance(mpn, list) and manufacturer[0] != mpn[0]:
                fail((sheet, mpn[0]), "PAIR_ROWS_DIFFER")
            elif isinstance(manufacturer, list) and isinstance(mpn, list):
                pair_columns.setdefault((sheet, manufacturer[1]), set()).add(mpn[1])
                pair_rows.append((sheet, manufacturer[0], manufacturer[1], mpn[1],
                                  manufacturer[2:] == ["part"] and mpn[2:] == ["part"]))
    _require(mapped_output == output_rows, "ROW_MAP_OUTPUT_COVERAGE_INVALID")

    # Unit 2b: every row on a lookup sheet that holds a maker or MPN for a line's part number is
    # carried by that line, so an alternate kept on another sheet cannot be left out silently.
    if lookup_keys:
        items = {out: _text(parsed.cells.get((out_sheet, out, item_column)) or "") for out in mapped_output}
        for look_sheet, key_column in sorted(lookup_keys):
            pair_columns_here = {column for (s, column) in column_roles if s == look_sheet}
            by_key: dict[str, set[tuple[str, int]]] = {}
            for (s, row), cols in source_rows.items():
                if s == look_sheet and cols & pair_columns_here and (s, row, key_column) in original.cells:
                    by_key.setdefault(_text(original.cells[(s, row, key_column)]), set()).add((s, row))
            for out, item in sorted(items.items()):
                for key in sorted(by_key.get(item, set()) - line_lookups.get(out, set())):
                    fail(key, "LOOKUP_ROW_NOT_CARRIED")

    # A052: the one bare board per assembly group given quantity 1 because the BOM prints none.
    def output_value(out: int, field: str) -> str:
        value = parsed.cells.get((out_sheet, out, columns.index(field) + 1))
        return "" if value is None else _text(value)

    def source_value(sheet: str, row: int, column: int) -> str:
        value = original.cells.get((sheet, row, column))
        return "" if value is None else _text(value)

    boards: Counter[tuple[str, Any]] = Counter()
    for out, sheet, rows, first, group in derived_quantities:
        pair_values = [output_value(out, f"{prefix}_{index}") for index in range(1, pairs + 1)
                       for prefix in ("manufacturer", "manufacturer_item_number")]
        if (output_value(out, "item_category") != "Part" or _number(output_value(out, "quantity")) != 1
                or not output_value(out, "bom_notes") or output_value(out, "reference_designator")
                or any(pair_values)):
            fail(first, BOARD_QUANTITY_INVALID)
            continue
        # The BOM's quantity column (the one other lines cite) must be empty on this line's rows.
        columns_here = {column for s, column in quantity_columns if s == sheet}
        if not columns_here or any(source_value(sheet, row, column)
                                   for row in rows for column in columns_here):
            fail(first, BOARD_QUANTITY_PRINTED)
            continue
        boards[(sheet, group)] += 1
        if boards[(sheet, group)] > 1:
            fail(first, BOARD_QUANTITY_REPEATED)

    # Sub-assembly structure (unit 2): code works out each line's parent from the cited cells and
    # compares it with the parser's claim. All lines or none, one kind, one column, one sheet.
    structure: dict[str, Any] = {"by": None, "lines_with_parent": 0, "depth": 0}
    first_rows = {line["output_row"]: (line["sheet"], min(line["rows"])) for line in lines}
    if claims:
        kinds = {claim["by"] for *_, claim in claims}
        sources = {(sheet, claim["from"][1]) for _, sheet, _, claim in claims}
        if len(claims) != len(lines):
            for out in sorted(mapped_output - {out for out, *_ in claims}):
                fail(first_rows[out], "STRUCTURE_INCOMPLETE")
        elif len(kinds) != 1 or len(sources) != 1:
            for out, sheet, row, _ in claims:
                fail((sheet, row), "STRUCTURE_INCONSISTENT")
        else:
            kind = next(iter(kinds))
            ordered = []
            for out, sheet, row, claim in sorted(claims, key=lambda item: item[2]):
                cell_row, cell_column = claim["from"]
                line_rows = next(line["rows"] for line in lines if line["output_row"] == out)
                key = (_structure_key(kind, original.cells.get((sheet, cell_row, cell_column)))
                       if cell_row in line_rows else None)
                if key is None:
                    fail((sheet, row), "STRUCTURE_VALUE_INVALID")
                    continue
                item = parsed.cells.get((out_sheet, out, item_column))
                ordered.append((out, key, _text(item).casefold() if item is not None else ""))
            if len(ordered) == len(claims):
                expected = _expected_parents(kind, ordered)
                claimed = {out: claim["line"] for out, _, _, claim in claims}
                for out, parent in expected.items():
                    if claimed[out] != parent:
                        fail(first_rows[out], "STRUCTURE_PARENT_MISMATCH")
                depth: dict[int, int] = {}
                for out, _, _ in ordered:
                    parent = expected[out]
                    depth[out] = 0 if parent is None else depth[parent] + 1
                structure = {"by": kind, "lines_with_parent": sum(1 for p in expected.values() if p is not None),
                             "depth": max(depth.values(), default=0)}

    # Manufacturer/MPN pattern on every line row (plan 8.12): a broken pattern is a finding.
    pattern: dict[tuple[str, int], str] = {}
    for key, roles in column_roles.items():
        if roles == {"manufacturer", "mpn"}:
            pattern.setdefault(key, "MANUFACTURER_MPN_SWAPPED")
    for key, mpns in pair_columns.items():
        if len(mpns) > 1:
            pattern.setdefault(key, "PAIR_COLUMNS_INCONSISTENT")
    consistent = {(sheet, mfr, next(iter(mpns))) for (sheet, mfr), mpns in pair_columns.items() if len(mpns) == 1}
    for sheet, mfr, mpn in consistent:
        for (row_sheet, row), cols in source_rows.items():
            if row_sheet != sheet or (sheet, row) not in owner:
                continue
            halves = [column for column in (mfr, mpn) if column in cols
                      and (sheet, row, column) not in cited_cells]
            if not halves:
                continue
            if len(halves) == 2 or (sheet, row, mfr if halves[0] == mpn else mpn) in pair_cells:
                fail((sheet, row), "PAIR_NOT_CARRIED")
            else:
                fail((sheet, row), "PAIR_HALF_MISSING")
    for sheet, row, mfr, mpn, combined in pair_rows:
        if not combined:
            continue
        names = [part for part in _PARTS.split(_text(original.cells[(sheet, row, mfr)])) if part]
        codes = [part for part in _PARTS.split(_text(original.cells[(sheet, row, mpn)])) if part]
        # One maker with several part numbers is one pair per part number, as a person reads it
        # (round 11, A043 row 81: "NICHTEK" with "PDCQ580JB-SMB, PDCQ580JU-SMB").
        single_maker = len(names) == 1 and len(codes) > 1
        if len(names) != len(codes) and not single_maker:
            fail((sheet, row), "COMBINED_PAIR_COUNT_MISMATCH")
        elif part_links[(sheet, row, mpn)] != len(codes) or (not single_maker and part_links[(sheet, row, mfr)] != len(names)):
            fail((sheet, row), "COMBINED_PART_NOT_CARRIED")

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
                if key in owner or key in looked_up:
                    fail(key, "ROW_IN_LINE_AND_SKIPPED")
                elif key in skipped_rows:
                    fail(key, "ROW_SKIPPED_TWICE")
                else:
                    skipped_rows[key] = entry["reason"]
    for key in source_rows:
        if key not in owner and key not in skipped_rows and key not in looked_up:
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
        elif (any((sheet, column) in column_roles for column in source_rows[key])
              and not any((sheet, column) in used_columns and (sheet, column) not in column_roles
                          for column in source_rows[key])):
            # Shaped like an alternate: a maker or MPN and nothing in any other mapped column (a
            # rank or other unused column may be filled). Heading and document rows also fill
            # description, quantity or part-number columns, so they stay skippable.
            fail(key, "SKIPPED_ROW_HOLDS_PAIR")

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
    # An ignored column holding manufacturer names seen in a mapped manufacturer column is a
    # manufacturer column the parser left out (repeated-pair layouts often reuse header text).
    makers = {_text(value).casefold() for (sheet, row, column), value in original.cells.items()
              if (sheet, row) in owner and "manufacturer" in column_roles.get((sheet, column), set()) and _text(value)}
    for sheet, column in sorted(ignored_columns):
        values = {_text(original.cells[(sheet, row, column)]).casefold()
                  for (row_sheet, row) in owner if row_sheet == sheet and (sheet, row, column) in original.cells}
        if values & makers:
            pattern.setdefault((sheet, column), "PAIR_COLUMN_IGNORED")

    recorded = sorted(failed.items())
    return {
        "version": RESULT_VERSION,
        "valid": not failed and not unmapped and not contradicted and not pattern,
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
        "field_mismatches": mismatches,
        "unlinked_fields": unlinked,
        "unmapped_columns": [{"sheet": sheet, "column": column}
                             for sheet, column in unmapped[:MAX_RECORDED_COLUMNS]],
        "ignored_but_used_columns": [{"sheet": sheet, "column": column}
                                     for sheet, column in contradicted[:MAX_RECORDED_COLUMNS]],
        "pattern_findings": [{"sheet": sheet, "column": column, "code": code}
                             for (sheet, column), code in sorted(pattern.items())[:MAX_RECORDED_COLUMNS]],
        # Information only: maker names written with a leading customer code (count, no values).
        "maker_code_prefix_kept": maker_codes_kept,
        # Sub-assembly structure proven from the cited cells (counts only; the claims stay in the map).
        "structure": structure,
        "lookup_rows": len(looked_up),
    }
