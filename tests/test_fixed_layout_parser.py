"""Hand-written layout parsers (bom_processor.fixed_layout_parser, Nick 2026-10-14).

Each test builds a parser from a synthetic layout config and runs it through the real offline
validation path (code policy, child-process worker, schema, semantic and row-accounting checks)
via operator_candidate.offline_validate. The shapes mirror the layouts the parsers were written
for, with synthetic labels and parts only:
- a clean single sheet with up to five optional maker/MPN pairs and a printed board quantity;
- a legacy .xls with reference rows (printed 0), alternate-source rows under parts and a second sheet;
- maker/MPN headers that are swapped relative to their data, and a part number split over two cells;
- a missing part number (the MPN becomes the item number), comments, and '/'-joined pairs;
- a board with no printed quantity (derived 1, flagged) and a real part with a blank quantity (stops).
Run: python -m pytest tests/test_fixed_layout_parser.py
"""
import io

import pytest
from openpyxl import Workbook

from bom_processor.candidate_authoring import _validate_code
from bom_processor.fixed_layout_parser import LayoutConfigError, build_parser, validate_config
from bom_processor.operator_candidate import offline_validate
from legacy_xls import legacy_xls

CLEAN = {"version": "fixed-layout-parser.v1", "layout": "synthetic clean", "sheet": "BOM", "header_row": 1,
         "columns": {"item_number": "Part No", "revision": "Rev", "item_name": "Description", "quantity": "Qty",
                     "reference_designator": "Ref Des", "category": "Kind", "comment": "Remarks"},
         "pairs": [[f"Maker {n}", f"Maker PN {n}"] for n in range(1, 6)], "min_pairs": 1,
         "board_words": ["fabrication"], "ignored_reasons": {"Item": "line position only"}}


def xlsx(sheets):
    book = Workbook()
    book.remove(book.active)
    for name, rows in sheets.items():
        sheet = book.create_sheet(name)
        for row in rows:
            sheet.append(row)
    stream = io.BytesIO()
    book.save(stream)
    return stream.getvalue()


def check(config, source, extension=".xlsx"):
    result = offline_validate(source, extension, build_parser(config))
    result["by_item"] = {row["item_number"]: row for row in result.get("rows", [])}
    return result


def clean_rows(pairs):
    heading = ["Item", "Part No", "Rev", "Description", "Qty", "Ref Des", "Kind"]
    for n in range(1, pairs + 1):
        heading += [f"Maker {n}", f"Maker PN {n}"]
    heading.append("Remarks")
    pad = [None] * (2 * pairs - 2)
    return [heading,
            [1, "FAB-100", "02", "PCB FABRICATION, TEST BOARD", 1, None, "PCB", None, None, *pad, None],
            [2, "CAP-1", None, "CAP 1UF", 3, "C1,C2,C3", "CAP", "MAKER A", "MPN-A1", *pad, None],
            [3, None, None, "RES 10K", 1, "R1", "RES", "MAKER B", "MPN-B1", *pad, "check value"]]


def test_the_generated_parser_passes_the_authoring_code_policy():
    _validate_code(build_parser(CLEAN), "xlsx")


@pytest.mark.parametrize("pairs", [1, 3, 5])
def test_one_clean_parser_reads_every_pair_count_and_keeps_the_printed_board(pairs):
    rows = clean_rows(pairs)
    if pairs > 1:
        rows[2][9:11] = ["MAKER C", "MPN-C1"]
    result = check(CLEAN, xlsx({"BOM": rows}))
    assert result["state"] == "awaiting_approval", result
    board = result["by_item"]["FAB-100"]
    assert (board["item_category"], board["quantity"], board["revision"]) == ("Part", 1, "02")
    assert board["bom_notes"] is None
    cap = result["by_item"]["CAP-1"]
    assert (cap["manufacturer_1"], cap["manufacturer_item_number_1"]) == ("MAKER A", "MPN-A1")
    if pairs > 1:
        assert (cap["manufacturer_2"], cap["manufacturer_item_number_2"]) == ("MAKER C", "MPN-C1")
    # No part number: the MPN is the item number, said in bom_notes with the row's own remark.
    res = result["by_item"]["MPN-B1"]
    assert "BOM comment: check value" in res["bom_notes"]
    assert "manufacturer part number is used as its item number" in res["bom_notes"]
    assert result["total_part_quantity"] == 5


def test_a_missing_heading_fails_loudly_instead_of_guessing():
    rows = clean_rows(1)
    rows[0][4] = "Count"
    assert check(CLEAN, xlsx({"BOM": rows}))["state"].startswith("failed:")


def test_a_real_part_with_a_blank_quantity_stops_and_is_never_counted_from_designators():
    rows = clean_rows(1)
    rows[2][4] = None
    result = check(CLEAN, xlsx({"BOM": rows}))
    assert result["state"] == "failed:SCHEMA_INVALID"
    cap = result["by_item"]["CAP-1"]
    assert (cap["item_category"], cap["quantity"]) == ("Part", None)


def test_slash_joined_makers_are_kept_as_written_and_flagged():
    rows = clean_rows(1)
    rows[2][7:9] = ["MAKER A/MAKER B", "MPN-A1/MPN-B9"]
    result = check(CLEAN, xlsx({"BOM": rows}))
    assert result["state"] == "awaiting_approval"
    cap = result["by_item"]["CAP-1"]
    assert cap["manufacturer_item_number_1"] == "MPN-A1/MPN-B9"
    assert "joined by '/'" in cap["bom_notes"]


# A legacy .xls MOD-style layout: reference rows with a printed 0, the board printed 1, alternate
# sources on rows of their own under a part, numeric MPNs stored as floats, and a second sheet.
MOD = {"version": "fixed-layout-parser.v1", "layout": "synthetic mod", "sheet": "PARTS", "header_row": 1,
       "columns": {"item_number": "Cust PN", "revision": "Doc Rev", "item_name": "Text", "quantity": "Count",
                   "reference_designator": "Refs", "unit_of_measure": "Unit", "category": "Group"},
       "pairs": [["Maker", "Maker Part"]], "board_words": ["fabrication"], "ignored_reasons": {}}
MOD_ROWS = [["Line", "Lvl", "Site PN", "Cust PN", "Doc Rev", "Count", "Maker", "Maker Part", "Refs", "Text",
             "Group", "Unit"],
            [1, 0, "S-ASM-1", "ASM-1", "01", 0, None, None, None, "PCB ASSY, TEST", None, None],
            [2, None, "S-ART-1", "ART-1", "01", 0, None, None, None, "PCB ARTWORK, TEST", None, None],
            [3, None, "S-FAB-1", "FAB-1", "01", 1, None, None, "PCB", "PCB FABRICATION, TEST", "PCB", "EA"],
            [4, 1, "S-CAP-1", "CAP-1", None, 4, "MAKER A", "MPN-A10", "C1,C2,C3,C4", "CAP 10PF", "CAP", "EA"],
            [None, None, None, None, None, None, "MAKER B", "MPN-B10", None, "CAP 10PF", "CAP", "EA"],
            [None, None, None, None, None, None, "MAKER D", 1469751.0, None, "CAP 10PF", "CAP", "EA"],
            [5, 1, "S-CON-1", "CON-1", None, 1, "MAKER C", 430451028.0, "J1", "CONN 10POS", "CONN", "EA"]]


def test_mod_layout_reads_documents_alternates_board_and_numeric_mpns_from_a_legacy_xls():
    source = legacy_xls({"PARTS": MOD_ROWS, "SPARE": [["Line", "Note"], [1, "not placed"]]})
    result = check(MOD, source, ".xls")
    assert result["state"] == "awaiting_approval", result
    rows = result["by_item"]
    assert [(rows[key]["item_category"], rows[key]["quantity"]) for key in ("ASM-1", "ART-1", "FAB-1")] == [
        ("Document", 0), ("Document", 0), ("Part", 1)]
    cap = rows["CAP-1"]
    assert [(cap[f"manufacturer_{n}"], cap[f"manufacturer_item_number_{n}"]) for n in (1, 2, 3)] == [
        ("MAKER A", "MPN-A10"), ("MAKER B", "MPN-B10"), ("MAKER D", "1469751")]
    assert rows["CON-1"]["manufacturer_item_number_1"] == "430451028"
    assert result["lines"] == 5 and result["alternate_pairs"] == 2


def test_a_board_with_no_printed_quantity_gets_1_derived_and_flagged():
    rows = [list(row) for row in MOD_ROWS]
    rows[3][5] = None
    rows[3][8] = None
    result = check(MOD, legacy_xls({"PARTS": rows}), ".xls")
    assert result["state"] == "awaiting_approval", result
    board = result["by_item"]["FAB-1"]
    assert (board["item_category"], board["quantity"]) == ("Part", 1)
    assert board["bom_notes"].startswith("Selected as this assembly's bare board")
    assert result["derived_board"] == 1


SWAPPED = {"version": "fixed-layout-parser.v1", "layout": "synthetic swapped", "sheet": None, "header_row": 1,
           "columns": {"item_number": "PN", "item_name": "Desc", "quantity": "Qty",
                       "reference_designator": "Refs"},
           # The data under "Maker n" is the part number and under "Maker PN n" the maker.
           "pairs": [["Maker PN 1", "Maker 1"], ["Maker PN 2", "Maker 2"]], "board_words": [],
           "ignored_reasons": {}}


def test_swapped_pair_headers_are_read_by_role_and_a_split_part_number_is_flagged_not_joined():
    rows = [["PN", "Desc", "Qty", "Refs", "Maker 1", "Maker PN 1", "Maker 2", "Maker PN 2"],
            ["D-1", "DIODE", 2, "D1,D2", "MPN-D1", "MAKER E", None, None],
            ["D-2", "DIODE 2", 1, "D3", "MPN-D2", None, "115", "MAKER F"]]
    result = check(SWAPPED, xlsx({"Only sheet": rows}))
    assert result["state"] == "awaiting_approval", result
    first = result["by_item"]["D-1"]
    assert (first["manufacturer_1"], first["manufacturer_item_number_1"]) == ("MAKER E", "MPN-D1")
    split = result["by_item"]["D-2"]
    assert (split["manufacturer_1"], split["manufacturer_item_number_1"]) == (None, "MPN-D2")
    assert (split["manufacturer_2"], split["manufacturer_item_number_2"]) == ("MAKER F", "115")
    assert "may split one part number across two cells ('MPN-D2,115')" in split["bom_notes"]


@pytest.mark.parametrize("change", [
    {"version": "other"}, {"header_row": 0}, {"columns": {"item_name": "A", "quantity": "B"}},
    {"pairs": []}, {"min_pairs": 6}, {"extra": 1},
    {"columns": {"item_number": "Same", "item_name": "Same", "quantity": "Q"}},
])
def test_unclear_configs_are_refused(change):
    with pytest.raises(LayoutConfigError):
        validate_config({**CLEAN, **change})
