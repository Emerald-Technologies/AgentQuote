"""Offline inert spreadsheet inventory; input bytes, output cell facts only.

No network, formulas, macros or parser execution. Run interpretation tests with
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

from .master_registry import html_table_raw_rows

EXTRACTION_VERSION = "bom-source-inventory.v1"
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


def _zip_guard(data: bytes) -> None:
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
            if any("vba" in name.casefold() or "externallinks/" in name.casefold() for name in names):
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


def extract_inventory(data: bytes, extension: str) -> SourceInventory:
    """Read every supported sheet; limit/unsupported content is never truncated."""
    if not isinstance(data, bytes) or not 0 < len(data) <= MAX_BYTES:
        raise InventoryError("SOURCE_SIZE_LIMIT")
    cells: dict[tuple[str, int, int], Any] = {}
    sheets: list[str] = []
    merged: list[tuple[str, str]] = []
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

    extension = extension.casefold()
    if extension == ".xlsx":
        from openpyxl import load_workbook
        _zip_guard(data)
        # ZIP/dimension bounds precede cell traversal; merged anchors remain
        # explicit, never silently filled into other cells.
        book = load_workbook(io.BytesIO(data), data_only=False, keep_links=False)
        try:
            for sheet in book.worksheets:
                if sheet.max_row > MAX_ROWS or sheet.max_column > MAX_COLUMNS:
                    raise InventoryError("SOURCE_DIMENSION_LIMIT")
                if any(cell.data_type in {"f", "e"} for row in sheet for cell in row):
                    raise InventoryError("SOURCE_FORMULA_OR_ERROR")
                merged.extend((sheet.title, str(region)) for region in sheet.merged_cells.ranges)
                add_sheet(sheet.title, sheet.iter_rows(values_only=True), sheet.max_row, sheet.max_column)
        finally:
            book.close()
    elif extension == ".xls" and data.startswith(b"\xd0\xcf\x11\xe0"):
        # Reject before allocating binary workbook structures: cached values
        # are not sufficient formula provenance in this first slice.
        raise InventoryError("BINARY_XLS_FORMULA_PROVENANCE_UNSUPPORTED")
    elif extension == ".xls":
        rows = html_table_raw_rows(data)
        add_sheet("HTML_TABLE_1", rows, len(rows), max(map(len, rows), default=0))
    else:
        raise InventoryError("SOURCE_FORMAT_UNSUPPORTED")
    return SourceInventory(sha256(data).hexdigest(), len(data), tuple(sheets), cells, tuple(merged))