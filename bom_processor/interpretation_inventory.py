"""Offline inert spreadsheet inventory; input bytes, output cell facts only.

No network, macros or parser execution, and formulas are never evaluated: a
formula cell contributes the result Excel stored in the file, and its location
is recorded in formula_cells. Run interpretation tests with
python -m pytest tests/test_interpretation_coverage.py from repository root.
"""
from __future__ import annotations

import io
import math
import zipfile
from xml.etree import ElementTree
from dataclasses import dataclass
from hashlib import sha256
from typing import Any

from .master_registry import html_table_raw_rows, RegistryError
from .source_format import is_macro_enabled, reading_extension

# v2: formula cells carry their stored (cached) result and are listed in
# formula_cells; v1 refused every workbook containing a formula.
EXTRACTION_VERSION = "bom-source-inventory.v2"
MAX_BYTES = 32 * 1024 * 1024
MAX_ROWS = 100_000
MAX_COLUMNS = 512
MAX_CELLS = 100_000
MAX_SHEETS = 32


class InventoryError(ValueError):
    """Stable non-sensitive source extraction failure."""


@dataclass(frozen=True)
class SourceInventory:
    sha256: str
    size_bytes: int
    sheets: tuple[str, ...]
    cells: dict[tuple[str, int, int], str | int | float | bool]
    merged_ranges: tuple[tuple[str, str], ...] = ()
    # Cells whose value is the stored result of a formula (never evaluated here).
    formula_cells: frozenset[tuple[str, int, int]] = frozenset()
    # False for binary .xls: the reader exposes stored values only, so which
    # cells held formulas is unknown (never assumed to be none).
    formula_cells_known: bool = True


def _zip_guard(data: bytes, *, macros_ignored: bool = False) -> None:
    """Bound the package before openpyxl reads it. VBA parts are refused, except in a .xlsm
    (macros_ignored), where they are left unread: openpyxl never loads them (keep_vba=False).
    External link parts are always refused."""
    from openpyxl.utils.cell import coordinate_to_tuple, range_boundaries
    traversal_budget = 0
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            names = [entry.filename for entry in entries]
            if (len(entries) > 256 or len(names) != len(set(names))
                    or sum(entry.file_size for entry in entries) > MAX_BYTES
                    or any(entry.file_size > 8 * 1024 * 1024 or entry.flag_bits & 1 for entry in entries)):
                raise InventoryError("SOURCE_ZIP_LIMIT")
            if any(("vba" in name.casefold() and not macros_ignored) or "externallinks/" in name.casefold()
                   for name in names):
                raise InventoryError("SOURCE_ACTIVE_OR_EXTERNAL_CONTENT")
            for entry in entries:
                if entry.filename.endswith(".xml"):
                    content = archive.read(entry)
                    if b"<!DOCTYPE" in content.upper() or b"<!ENTITY" in content.upper():
                        raise InventoryError("SOURCE_XML_DECLARATION")
                    if entry.filename.startswith("xl/worksheets/") and entry.filename.endswith(".xml"):
                        # Bound rectangular and merged-cell allocation before
                        # openpyxl materializes any worksheet cells.
                        max_row = max_col = 1
                        for _, element in ElementTree.iterparse(io.BytesIO(content), events=("end",)):
                            tag = element.tag.rsplit("}", 1)[-1]
                            if tag == "c":
                                row, col = coordinate_to_tuple(element.attrib["r"])
                                max_row, max_col = max(max_row, row), max(max_col, col)
                            elif tag == "mergeCell":
                                left, top, right, bottom = range_boundaries(element.attrib["ref"])
                                if (right - left + 1) * (bottom - top + 1) > MAX_CELLS:
                                    raise InventoryError("SOURCE_TRAVERSAL_LIMIT")
                                max_row, max_col = max(max_row, bottom), max(max_col, right)
                            element.clear()
                        traversal_budget += max_row * max_col
                        if max_row > MAX_ROWS or max_col > MAX_COLUMNS or traversal_budget > MAX_CELLS:
                            raise InventoryError("SOURCE_TRAVERSAL_LIMIT")
    except (zipfile.BadZipFile, RuntimeError, ElementTree.ParseError, KeyError, ValueError) as exc:
        if isinstance(exc, InventoryError):
            raise
        raise InventoryError("SOURCE_WORKBOOK_INVALID") from exc


def _binary_xls(data: bytes, add_sheet: Any) -> None:
    """Stored values of a binary (OLE2) workbook; no macros, formulas or links run."""
    import xlrd
    try:
        book = xlrd.open_workbook(file_contents=data, on_demand=True, formatting_info=False)
    except Exception as exc:
        raise InventoryError("SOURCE_WORKBOOK_INVALID") from exc
    try:
        if book.nsheets > MAX_SHEETS:
            raise InventoryError("SOURCE_DIMENSION_LIMIT")
        traversal = 0
        for index in range(book.nsheets):
            sheet = book.sheet_by_index(index)
            traversal += sheet.nrows * sheet.ncols
            if sheet.nrows > MAX_ROWS or sheet.ncols > MAX_COLUMNS or traversal > MAX_CELLS:
                raise InventoryError("SOURCE_TRAVERSAL_LIMIT")
            rows = []
            for row in range(sheet.nrows):
                values: list[Any] = []
                for column in range(sheet.ncols):
                    kind, value = sheet.cell_type(row, column), sheet.cell_value(row, column)
                    if kind == xlrd.XL_CELL_ERROR:
                        raise InventoryError("SOURCE_ERROR_CELL")
                    if kind == xlrd.XL_CELL_BOOLEAN:
                        value = bool(value)
                    elif kind in (xlrd.XL_CELL_NUMBER, xlrd.XL_CELL_DATE) and float(value).is_integer():
                        # The format stores every number as a float; 13.0 is shown as 13.
                        value = int(value)
                    values.append(value)
                rows.append(values)
            add_sheet(sheet.name, rows, sheet.nrows, sheet.ncols)
            book.unload_sheet(index)
    finally:
        book.release_resources()


def extract_inventory(data: bytes, extension: str) -> SourceInventory:
    """Read every supported sheet; limit/unsupported content is never truncated."""
    if not isinstance(data, bytes) or not 0 < len(data) <= MAX_BYTES:
        raise InventoryError("SOURCE_SIZE_LIMIT")
    cells: dict[tuple[str, int, int], Any] = {}
    sheets: list[str] = []
    merged: list[tuple[str, str]] = []
    formulas: set[tuple[str, int, int]] = set()
    total_rows = 0

    def add_sheet(name: str, rows: Any, row_count: int, column_count: int) -> None:
        nonlocal total_rows
        total_rows += row_count
        if (name in sheets or len(sheets) >= MAX_SHEETS or total_rows > MAX_ROWS
                or column_count > MAX_COLUMNS):
            raise InventoryError("SOURCE_DIMENSION_LIMIT")
        sheets.append(name)
        for row_index, row in enumerate(rows, 1):
            for column, value in enumerate(row, 1):
                if value is None or value == "":
                    continue
                if (not isinstance(value, (str, int, float, bool))
                        or isinstance(value, float) and not math.isfinite(value)):
                    raise InventoryError("SOURCE_VALUE_UNSUPPORTED")
                if len(cells) >= MAX_CELLS:
                    raise InventoryError("SOURCE_CELL_LIMIT")
                cells[(name, row_index, column)] = value

    # A .xlsm is the same OOXML package and is read as .xlsx. Its macros are ignored (QQ #99 D1):
    # _zip_guard lets its VBA part through unread and openpyxl never loads it (keep_vba=False).
    # Any other workbook with a VBA part, and any workbook with external links, is still refused.
    macros_ignored = is_macro_enabled(extension)
    extension = reading_extension(extension)
    if extension == ".xlsx":
        from openpyxl import load_workbook
        _zip_guard(data, macros_ignored=macros_ignored)
        # ZIP/dimension bounds precede cell traversal; merged anchors remain
        # explicit, never silently filled into other cells.
        book = load_workbook(io.BytesIO(data), data_only=False, keep_links=False, keep_vba=False)
        # The same bytes opened for stored results; formulas are never evaluated.
        cached = load_workbook(io.BytesIO(data), data_only=True, keep_links=False, keep_vba=False)
        try:
            for sheet in book.worksheets:
                if sheet.max_row > MAX_ROWS or sheet.max_column > MAX_COLUMNS:
                    raise InventoryError("SOURCE_DIMENSION_LIMIT")
                stored = cached[sheet.title]
                rows: list[list[Any]] = []
                for row in sheet.iter_rows():
                    values: list[Any] = []
                    for cell in row:
                        if cell.data_type == "e":
                            raise InventoryError("SOURCE_ERROR_CELL")
                        if cell.data_type != "f":
                            values.append(cell.value)
                            continue
                        result = stored.cell(cell.row, cell.column)
                        # A missing stored result is unknowable without evaluation.
                        if result.value is None or result.value == "":
                            raise InventoryError("SOURCE_FORMULA_RESULT_MISSING")
                        if result.data_type == "e":
                            raise InventoryError("SOURCE_ERROR_CELL")
                        formulas.add((sheet.title, cell.row, cell.column))
                        values.append(result.value)
                    rows.append(values)
                merged.extend((sheet.title, str(region)) for region in sheet.merged_cells.ranges)
                add_sheet(sheet.title, rows, sheet.max_row, sheet.max_column)
        finally:
            book.close()
            cached.close()
    elif extension == ".xls" and data.startswith(b"\xd0\xcf\x11\xe0"):
        _binary_xls(data, add_sheet)
        return SourceInventory(sha256(data).hexdigest(), len(data), tuple(sheets), cells, (),
                               frozenset(), formula_cells_known=False)
    elif extension == ".xls":
        try:
            rows = html_table_raw_rows(data)
        except RegistryError as exc:
            raise InventoryError("SOURCE_HTML_INVALID") from exc
        add_sheet("HTML_TABLE_1", rows, len(rows), max(map(len, rows), default=0))
    else:
        raise InventoryError("SOURCE_FORMAT_UNSUPPORTED")
    return SourceInventory(sha256(data).hexdigest(), len(data), tuple(sheets), cells, tuple(merged),
                           frozenset(formulas))