"""Read verified resolved workbook bytes inertly; never parse customer BOM sources.

Inputs: approved assembly metadata and workbook byte map keyed by document UUID.
Outputs: Part occurrences, Document locators and complete packet-only row observations.
Run python -m pytest tests/test_package_evidence_occurrences.py.
"""
from __future__ import annotations

import io
import re
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from xml.etree import ElementTree as ET

import openpyxl

from .canonical_format import MAX_MANUFACTURER_PAIRS, canonical_columns, canonical_pair_count
from .package_evidence_contracts import (
    PackageEvidenceError, assemblies_metadata, detached, integer, nullable_text,
    occurrence_id, require, semantic_key, text,
)
from .package_evidence_inventory import ArchiveBudget, InventoryLimits, read_zip, verified_bytes


def quantity_text(value) -> str | None:
    if value is None:
        return None
    require(type(value) in {int, float, str, Decimal}, "INVALID_QUANTITY")
    if isinstance(value, str):
        require(0 < len(value) <= 128 and value == value.strip(), "INVALID_QUANTITY")
    try:
        number = Decimal(str(value))
        require(number.is_finite() and number >= 0, "INVALID_QUANTITY")
        # Bound before formatting to prevent gigantic exponent expansion.
        require(number.is_zero() or (-128 <= number.adjusted() <= 127
                and number.as_tuple().exponent >= -128), "QUANTITY_LIMIT")
        result = "0" if number.is_zero() else format(number, "f")
        if "." in result:
            result = result.rstrip("0").rstrip(".")
        require(len(result) <= 128 and re.fullmatch(r"(?:0|[1-9][0-9]*)(?:\.[0-9]*[1-9])?",
                                                    result) is not None, "INVALID_QUANTITY")
        return result
    except (InvalidOperation, ValueError, OverflowError) as exc:
        raise PackageEvidenceError("INVALID_QUANTITY") from exc


def _xml_text(payload: bytes) -> str:
    """Strictly decode supported XML encodings before any XML parser sees content.

    Byte bounds alone do not make ElementTree safe against internal entities.
    Restrict encodings so the declaration screen and downstream parsers cannot
    interpret different text. DTDs/entities are forbidden even in unused parts.
    """
    require(not payload.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff",
                                    b"\x00\x00\x00<", b"<\x00\x00\x00")),
            "WORKBOOK_XML_ENCODING")
    if payload.startswith(b"\xff\xfe"):
        encoding, content = "utf-16-le", payload[2:]
    elif payload.startswith(b"\xfe\xff"):
        encoding, content = "utf-16-be", payload[2:]
    elif payload.startswith(b"<\x00"):
        encoding, content = "utf-16-le", payload
    elif payload.startswith(b"\x00<"):
        encoding, content = "utf-16-be", payload
    else:
        encoding, content = "utf-8", payload.removeprefix(b"\xef\xbb\xbf")
    try:
        decoded = content.decode(encoding, errors="strict")
    except UnicodeError as exc:
        raise PackageEvidenceError("WORKBOOK_XML_ENCODING") from exc
    require("\x00" not in decoded, "WORKBOOK_XML_ENCODING")
    require(re.search(r"<!\s*(?:doctype|entity)\b", decoded, re.I) is None,
            "WORKBOOK_XML_UNSAFE")
    if decoded.startswith("<?xml"):
        declaration = re.match(
            r'''<\?xml\s+version\s*=\s*(?:"1\.[01]"|'1\.[01]')'''
            r'''(?:\s+encoding\s*=\s*(?:"([A-Za-z][A-Za-z0-9._-]*)"|'([A-Za-z][A-Za-z0-9._-]*)'))?'''
            r'''(?:\s+standalone\s*=\s*(?:"(?:yes|no)"|'(?:yes|no)'))?\s*\?>''', decoded)
        require(declaration is not None, "WORKBOOK_XML_INVALID")
        declared = (declaration.group(1) or declaration.group(2) or encoding).lower()
        supported = {encoding} if encoding == "utf-8" else {encoding, "utf-16"}
        require(declared in supported, "WORKBOOK_XML_ENCODING")
    return decoded


def _preflight(data: bytes, *, max_sheets: int, max_rows: int) -> None:
    parts = read_zip(data, ArchiveBudget(InventoryLimits()))
    names = {name for name, _ in parts}
    require({"[Content_Types].xml", "xl/workbook.xml"} <= names, "WORKBOOK_INVALID")
    sheets = rows = 0
    for name, payload in parts:
        lowered = name.lower()
        require("vbaproject" not in lowered and not lowered.startswith("xl/externallinks/")
                and not lowered.startswith("xl/macrosheets/"), "WORKBOOK_ACTIVE_CONTENT")
        if not (lowered.endswith(".xml") or lowered.endswith(".rels")):
            continue
        decoded = _xml_text(payload)
        try:
            root = ET.fromstring(decoded)
        except ET.ParseError as exc:
            raise PackageEvidenceError("WORKBOOK_XML_INVALID") from exc
        row_locations = set()
        cell_locations = set()
        if lowered.startswith("xl/worksheets/"):
            previous_row = 0
            for row_element in root.iter():
                if row_element.tag.rsplit("}", 1)[-1] != "row":
                    continue
                locator = row_element.attrib.get("r", "")
                require(locator.isascii() and locator.isdigit(), "WORKBOOK_ROW_LOCATOR")
                row_number = int(locator)
                require(row_number <= max_rows + 1, "WORKBOOK_ROW_LIMIT")
                require(previous_row < row_number, "WORKBOOK_ROW_LOCATOR")
                previous_row = row_number
                for cell in row_element:
                    if cell.tag.rsplit("}", 1)[-1] == "c":
                        match = re.fullmatch(r"[A-Z]{1,3}([1-9][0-9]{0,6})", cell.attrib.get("r", ""))
                        require(match is not None and int(match.group(1)) == row_number,
                                "WORKBOOK_CELL_LOCATOR")
        for element in root.iter():
            local = element.tag.rsplit("}", 1)[-1]
            require(local not in {"f", "externalReference"}, "WORKBOOK_FORMULA_OR_LINK")
            require(not (local == "Relationship" and
                         element.attrib.get("TargetMode", "").lower() == "external"),
                    "WORKBOOK_EXTERNAL_LINK")
            if local == "Override":
                content_type = element.attrib.get("ContentType", "").lower()
                require("macroenabled" not in content_type and "vba" not in content_type,
                        "WORKBOOK_ACTIVE_CONTENT")
            if name == "xl/workbook.xml" and local == "sheet":
                sheets += 1
                require(sheets <= max_sheets, "WORKBOOK_SHEET_LIMIT")
            if lowered.startswith("xl/worksheets/") and local == "row":
                rows += 1
                require(rows <= max_rows + max_sheets, "WORKBOOK_ROW_LIMIT")
                locator = element.attrib.get("r", "")
                require(locator.isascii() and locator.isdigit()
                        and 1 <= int(locator) <= max_rows + 1, "WORKBOOK_ROW_LIMIT")
                require(int(locator) not in row_locations, "WORKBOOK_DUPLICATE_ROW")
                row_locations.add(int(locator))
            if lowered.startswith("xl/worksheets/") and local == "c":
                locator = element.attrib.get("r", "")
                match = re.fullmatch(r"([A-Z]{1,3})([1-9][0-9]{0,6})", locator)
                require(match is not None, "WORKBOOK_CELL_LOCATOR")
                column = 0
                for char in match.group(1):
                    column = column * 26 + ord(char) - ord("A") + 1
                require(column <= len(canonical_columns(MAX_MANUFACTURER_PAIRS))
                        and int(match.group(2)) <= max_rows + 1, "WORKBOOK_ROW_SCHEMA")
                require(locator not in cell_locations, "WORKBOOK_DUPLICATE_CELL")
                cell_locations.add(locator)
    require(sheets > 0, "WORKBOOK_INVALID")


# Mirrors QuickQuote PACKAGE_EVIDENCE_REFERENCE_DESIGNATORS_MAX: long BOM lines list hundreds
# of designators (one real line has 2,218 characters); the graph keeps the full text.
REFERENCE_DESIGNATORS_MAX = 20_000


def _js_text(value):
    """A numeric revision as JavaScript String(number) spells it; other values unchanged."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return value
    if isinstance(value, float) and value.is_integer() and abs(value) < 1e21:
        return str(int(value))
    return str(value) if isinstance(value, int) else repr(value)


def read_occurrences(assemblies: list[dict], workbooks: dict[str, bytes], *,
                     max_sheets: int = 50, max_rows: int = 100_000) -> dict:
    integer(max_sheets, 1, 50)
    integer(max_rows, 1, 100_000)
    assemblies = assemblies_metadata(assemblies)
    require(type(workbooks) is dict, "INVALID_WORKBOOKS")
    expected = {item["canonicalBom"]["documentId"] for item in assemblies
                if item["canonicalBom"] is not None}
    require(set(workbooks) == expected, "WORKBOOK_COVERAGE_MISMATCH")
    verified = {}
    # Verify all outputs before any workbook is loaded.
    for assembly in assemblies:
        bom = assembly["canonicalBom"]
        if bom is not None:
            verified[bom["documentId"]] = verified_bytes(bom, workbooks[bom["documentId"]], 50_000_000)
    parsed = {}
    for document_id, data in verified.items():
        _preflight(data, max_sheets=max_sheets, max_rows=max_rows)
        workbook = None
        try:
            workbook = openpyxl.load_workbook(io.BytesIO(data), read_only=True,
                                             data_only=False, keep_links=False, keep_vba=False)
            require(len(workbook.worksheets) <= max_sheets, "WORKBOOK_SHEET_LIMIT")
            output = []
            count = 0
            for sheet in workbook.worksheets:
                text(sheet.title)
                # Ignore untrusted dimension declarations; actual XML rows are bounded above.
                sheet.reset_dimensions()
                iterator = sheet.iter_rows()
                header = next(iterator, ())
                pair_count = canonical_pair_count(cell.value for cell in header)
                require(pair_count is not None, "WORKBOOK_HEADER")
                columns = canonical_columns(pair_count)
                line = 0
                for row_number, cells in enumerate(iterator, 2):
                    require(row_number <= max_rows + 1, "WORKBOOK_ROW_LIMIT")
                    values = [cell.value for cell in cells]
                    if all(value is None for value in values):
                        continue
                    count += 1
                    require(count <= max_rows, "WORKBOOK_ROW_LIMIT")
                    require(len(values) <= len(columns), "WORKBOOK_ROW_SCHEMA")
                    values += [None] * (len(columns) - len(values))
                    require(all(cell.data_type not in {"f", "e"} for cell in cells),
                            "WORKBOOK_CELL_INVALID")
                    require(not any(isinstance(value, (bool, date, datetime, time)) for value in values),
                            "WORKBOOK_CELL_INVALID")
                    # A numeric revision cell (canonical BOMs carry revision 1) is read as QuickQuote
                    # reads it: String(number). Every other text column stays strictly text.
                    row = dict(zip(columns, values))
                    row["revision"] = _js_text(row["revision"])
                    line += 1
                    require(type(row["line_number"]) in {int, float}
                            and row["line_number"] == line, "WORKBOOK_LINE_NUMBER")
                    require(row["item_category"] in {"Part", "Document"}, "WORKBOOK_CATEGORY")
                    text(row["item_number"])
                    text(row["item_name"], 2000, controls=True)
                    nullable_text(row["revision"])
                    nullable_text(row["reference_designator"], REFERENCE_DESIGNATORS_MAX, controls=True)
                    for key in columns:
                        if key not in {"line_number", "quantity", "item_number", "item_name",
                                       "revision", "reference_designator", "item_category"}:
                            nullable_text(row[key], 2000, controls=True)
                    row["quantity"] = quantity_text(row["quantity"])
                    output.append({"sheet": sheet.title, "row": row_number, **row,
                                   "_source_cells": values, "_pairs": pair_count})
            require(bool(output), "WORKBOOK_EMPTY")
            parsed[document_id] = output
        except PackageEvidenceError:
            raise
        except Exception as exc:
            raise PackageEvidenceError("WORKBOOK_INVALID") from exc
        finally:
            if workbook is not None:
                workbook.close()
    occurrences, documents, observations, verified_rows = [], [], [], []
    total_rows = 0
    for assembly in assemblies:
        bom = assembly["canonicalBom"]
        if bom is None:
            continue
        for row in parsed[bom["documentId"]]:
            total_rows += 1
            require(total_rows <= max_rows, "OCCURRENCE_ROW_LIMIT")
            common = {"assemblyUuid": assembly["assemblyUuid"], "bomDocumentId": bom["documentId"],
                      "bomSha256": bom["sha256"], "sheet": row["sheet"], "row": row["row"],
                      "partNumber": row["item_number"], "revision": row["revision"],
                      "quantity": row["quantity"], "referenceDesignators": row["reference_designator"]}
            verified_rows.append({**{key: common[key] for key in (
                "assemblyUuid", "bomDocumentId", "bomSha256", "sheet", "row")},
                "occurrenceId": occurrence_id(assembly["assemblyUuid"], bom["sha256"],
                    row["sheet"], row["row"]) if row["item_category"] == "Part" else None,
                "cells": row["_source_cells"]})
            observations.append({
                **{key: common[key] for key in ("assemblyUuid", "bomDocumentId", "bomSha256",
                                               "sheet", "row", "partNumber", "revision")},
                "itemCategory": row["item_category"], "itemName": row["item_name"],
                "bomNotes": row["bom_notes"], "unitOfMeasure": row["unit_of_measure"],
                "manufacturerPairs": [{f"manufacturer_{index}": row[f"manufacturer_{index}"],
                    f"manufacturer_item_number_{index}": row[f"manufacturer_item_number_{index}"]}
                    for index in range(1, row["_pairs"] + 1)],
            })
            if row["item_category"] == "Document":
                documents.append(common)
            else:
                occurrences.append({"occurrenceId": occurrence_id(assembly["assemblyUuid"],
                    bom["sha256"], row["sheet"], row["row"]),
                    "semanticKey": semantic_key(row["item_number"], row["revision"]), **common})
    return detached({"occurrences": sorted(occurrences, key=lambda item: item["occurrenceId"]),
                     "documentRows": documents, "rowObservations": observations,
                     "verifiedRows": verified_rows})