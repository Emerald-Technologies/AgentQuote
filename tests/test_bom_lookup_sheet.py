"""Values kept on another sheet keyed by part number (unit 2b, AMAT shape; Nick 2026-10-12).

A multi-level BOM sheet (LEVEL, PARENT_PART, star-indented part numbers) with manufacturer part
numbers on a separate MFG sheet, one row per MPN, and a part used under two sub-assemblies.
Synthetic data only."""
import copy
import io
import json

import pytest
from openpyxl import Workbook

from bom_processor.canonical_format import canonical_columns
from bom_processor.row_accounting import VERSION, RowMapError, verify_row_map

BOM_HEADER = ["ORIGINAL", "PARENT_PART", "PART_NUMBER", "INDENTED_PART_NUMBER", "LEVEL", "DESCRIPTION",
              "QUANTITY", "UOM"]
# part, parent, level, description, quantity (as text, like the export)
BOM = [("9000-00001", "9000-00001", "0", "TOP ASSY", "1.000"),
       ("9100-00001", "9000-00001", "1", "PCB ASSY A", "1.000"),
       ("9500-00001", "9100-00001", "2", "CAP 10UF", "4.000"),
       ("9500-00002", "9100-00001", "2", "RES 10K", "2.000"),
       ("9100-00002", "9000-00001", "1", "PCB ASSY B", "2.000"),
       ("9500-00001", "9100-00002", "2", "CAP 10UF", "3.000"),     # same part under a second parent
       ("9600-00001", "9000-00001", "1", "SCREW M3", "6.000")]      # no MPN: no MFG row
MFG_HEADER = ["ORIGINAL", "PART_NUMBER", "DESCRIPTION", "MFG_PART_NUM", "MANUFACTURER_NAME"]
MFG = [("9500-00001", "CAP 10UF", "CAP-10U-A", "MAKER ONE"), ("9500-00001", "CAP 10UF", "CAP-10U-B", "MAKER TWO"),
       ("9500-00002", "RES 10K", "RES-10K", "MAKER ONE")]


def xlsx(sheets):
    book = Workbook(); book.remove(book.active)
    for title, rows in sheets.items():
        sheet = book.create_sheet(title)
        for row in rows:
            sheet.append(row)
    data = io.BytesIO(); book.save(data); book.close()
    return data.getvalue()


def source():
    bom = [BOM_HEADER] + [["9000-00001", parent, part, "**" * int(level) + part, level, desc, qty, "EA"]
                          for part, parent, level, desc, qty in BOM]
    mfg = [MFG_HEADER] + [["9000-00001", part, desc, mpn, maker] for part, desc, mpn, maker in MFG]
    return xlsx({"BOM": bom, "MFG": mfg})


def mfg_rows(part):
    return [index + 2 for index, row in enumerate(MFG) if row[0] == part]


def build():
    columns = canonical_columns(8)
    out_rows, lines = [columns], []
    levels = [int(level) for _, _, level, _, _ in BOM]
    for index, (part, parent, level, desc, qty) in enumerate(BOM):
        source_row, out = index + 2, index + 2
        values = dict.fromkeys(columns)
        values.update(line_number=index + 1, item_category="Part", item_number=part, item_name=desc,
                      quantity=float(qty))
        fields = {"item_number": [source_row, 3], "item_name": [source_row, 6], "quantity": [source_row, 7]}
        for pair, mfg_row in enumerate(mfg_rows(part), 1):
            _, _, mpn, maker = MFG[mfg_row - 2]
            values[f"manufacturer_{pair}"], values[f"manufacturer_item_number_{pair}"] = maker, mpn
            fields[f"manufacturer_{pair}"] = {"lookup": ["MFG", mfg_row, 5], "key": 2}
            fields[f"manufacturer_item_number_{pair}"] = {"lookup": ["MFG", mfg_row, 4], "key": 2}
        out_rows.append([values[c] for c in columns])
        parent_index = next((j for j in range(index - 1, -1, -1) if levels[j] < levels[index]), None)
        lines.append({"output_row": out, "sheet": "BOM", "rows": [source_row], "fields": fields,
                      "parent": {"line": None if parent_index is None else parent_index + 2,
                                 "from": [source_row, 5], "by": "level"}})
    row_map = {"version": VERSION, "lines": lines,
               "skipped": [{"sheet": "BOM", "rows": [[1, 1]], "reason": "header", "rationale": "Headings."},
                           {"sheet": "MFG", "rows": [[1, 1]], "reason": "header", "rationale": "Headings."}],
               "ignored_columns": [{"sheet": "BOM", "column": c, "reason": "Structure copies and unit."}
                                   for c in (1, 2, 4, 8)]}
    return source(), xlsx({"Parsed": out_rows}), row_map


def run(src, out, row_map):
    return verify_row_map(src, ".xlsx", out, json.dumps(row_map).encode())


def test_mpns_on_a_manufacturer_sheet_are_proven_by_part_number():
    result = run(*build())
    assert result["valid"] is True, (result["failed_rows"], result["unmapped_columns"], result["pattern_findings"])
    # One MFG row may serve the same part under two sub-assemblies; 3 MFG rows in all.
    assert result["lookup_rows"] == 3
    assert result["structure"] == {"by": "level", "lines_with_parent": 6, "depth": 2}


def test_an_mpn_row_left_out_for_one_occurrence_of_a_part_fails():
    src, out, row_map = build()
    # The second occurrence of 9500-00001 (output row 7) drops its second MPN.
    fields = row_map["lines"][5]["fields"]
    del fields["manufacturer_2"], fields["manufacturer_item_number_2"]
    from openpyxl import load_workbook
    sheet = load_workbook(io.BytesIO(out)).active
    columns = canonical_columns(8)
    for name in ("manufacturer_2", "manufacturer_item_number_2"):
        sheet.cell(7, columns.index(name) + 1).value = None
    data = io.BytesIO(); sheet.parent.save(data)
    result = run(src, data.getvalue(), row_map)
    assert {"sheet": "MFG", "row": 3, "code": "LOOKUP_ROW_NOT_CARRIED"} in result["failed_rows"]


def test_a_lookup_row_of_another_part_fails_on_its_key():
    src, out, row_map = build()
    row_map["lines"][2]["fields"]["manufacturer_1"]["lookup"][1] = 4  # RES row cited for the CAP line
    row_map["lines"][2]["fields"]["manufacturer_item_number_1"]["lookup"][1] = 4
    assert "LOOKUP_KEY_MISMATCH" in run(src, out, row_map)["failure_codes"]


def test_maker_and_mpn_must_come_from_one_lookup_row():
    src, out, row_map = build()
    # Pair 1 takes its maker from the second MFG row and its MPN from the first; values match.
    row_map["lines"][2]["fields"]["manufacturer_1"] = {"lookup": ["MFG", 3, 5], "key": 2}
    from openpyxl import load_workbook
    sheet = load_workbook(io.BytesIO(out)).active
    sheet.cell(4, canonical_columns(8).index("manufacturer_1") + 1).value = "MAKER TWO"
    data = io.BytesIO(); sheet.parent.save(data)
    assert {"sheet": "MFG", "row": 2, "code": "PAIR_ROWS_DIFFER"} in run(src, data.getvalue(), row_map)["failed_rows"]


def test_unused_manufacturer_rows_must_be_accounted_and_not_both_looked_up_and_skipped():
    src, out, row_map = build()
    unused = copy.deepcopy(row_map)
    for line in unused["lines"]:
        if line["fields"]["item_number"] == [4, 3]:  # the RES line stops citing its MFG row
            line["fields"] = {k: v for k, v in line["fields"].items() if not k.startswith("manufacturer")}
    assert "LOOKUP_ROW_NOT_CARRIED" in run(src, out, unused)["failure_codes"]
    both = copy.deepcopy(row_map)
    both["skipped"].append({"sheet": "MFG", "rows": [[2, 2]], "reason": "other", "rationale": "Overlap."})
    assert "ROW_IN_LINE_AND_SKIPPED" in run(src, out, both)["failure_codes"]


def test_part_number_and_quantity_never_come_from_a_lookup_and_the_sheet_must_differ():
    src, out, row_map = build()
    bad = copy.deepcopy(row_map)
    bad["lines"][2]["fields"]["item_name"] = {"lookup": ["MFG", 2, 3], "key": 2}  # allowed: description
    assert run(src, out, bad)["valid"] is True
    bad["lines"][2]["fields"]["quantity"] = {"lookup": ["MFG", 2, 3], "key": 2}
    assert "LOOKUP_NOT_ALLOWED" in run(src, out, bad)["failure_codes"]
    same = copy.deepcopy(row_map)
    same["lines"][2]["fields"]["manufacturer_1"] = {"lookup": ["BOM", 4, 6], "key": 3}
    with pytest.raises(RowMapError, match="ROW_MAP_LINK_INVALID"):
        run(src, out, same)


def test_the_rules_describe_the_lookup_link():
    from bom_processor.candidate_authoring import ROW_MAP_RULE
    from bom_processor.document_role import BOM_READING_GUIDANCE
    from bom_processor.row_refinement import refinement_instructions
    assert '{\\"lookup\\": [sheet, row, column], \\"key\\": column}' in json.dumps(ROW_MAP_RULE)
    assert "keyed by the part number" in BOM_READING_GUIDANCE
    assert "LOOKUP_ROW_NOT_CARRIED" in refinement_instructions({"rows": []}, 1, 2)
