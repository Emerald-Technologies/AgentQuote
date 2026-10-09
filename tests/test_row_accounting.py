"""Synthetic offline row-accounting fixtures (plan 8.8 step 4a); no customer data."""
import copy
import io
import json

import pytest
from openpyxl import Workbook

from bom_processor.canonical_format import canonical_columns
from bom_processor.row_accounting import RowMapError, VERSION, verify_row_map


def xlsx(rows, title="BOM"):
    book = Workbook(); sheet = book.active; sheet.title = title
    for row in rows:
        sheet.append(row)
    data = io.BytesIO(); book.save(data); book.close()
    return data.getvalue()


SOURCE_ROWS = [
    ["Customer BOM 123-456 Rev A"],                                   # 1 title
    ["Find", "Part", "Description", "Qty", "MFG", "MPN", "Notes"],    # 2 header
    [1, "R-100", "Resistor 10k", 4, "Acme", "RC0603", "RoHS"],         # 3 line 1
    [None, None, None, None, "Beta", "RB0603"],                       # 4 alternate of line 1
    [2, "C-200", "Capacitor 1uF", 2, "Gamma", "GC0805"],              # 5 line 2
    [None, None, None, None, None, None],                             # 6 blank
    ["Total parts", None, None, 6],                                   # 7 total
]


def output(lines, pairs=8):
    columns = canonical_columns(pairs)
    rows = [columns]
    for number, values in enumerate(lines, 1):
        row = dict.fromkeys(columns)
        row.update(line_number=number, item_category="Part", **values)
        rows.append([row[column] for column in columns])
    return xlsx(rows, title="Parsed")


def good():
    source = xlsx(SOURCE_ROWS)
    out = output([
        {"item_number": "R-100", "item_name": "Resistor 10k", "quantity": 4, "bom_notes": "RoHS",
         "manufacturer_1": "Acme", "manufacturer_item_number_1": "RC0603",
         "manufacturer_2": "Beta", "manufacturer_item_number_2": "RB0603"},
        {"item_number": "C-200", "item_name": "Capacitor 1uF", "quantity": 2,
         "manufacturer_1": "Gamma", "manufacturer_item_number_1": "GC0805"},
    ])
    row_map = {"version": VERSION, "lines": [
        {"output_row": 2, "sheet": "BOM", "rows": [3, 4], "fields": {
            "item_number": [3, 2], "item_name": [3, 3], "quantity": [3, 4], "bom_notes": [3, 7],
            "manufacturer_1": [3, 5], "manufacturer_item_number_1": [3, 6],
            "manufacturer_2": [4, 5], "manufacturer_item_number_2": [4, 6]}},
        {"output_row": 3, "sheet": "BOM", "rows": [5], "fields": {
            "item_number": [5, 2], "item_name": [5, 3], "quantity": [5, 4],
            "manufacturer_1": [5, 5], "manufacturer_item_number_1": [5, 6]}},
    ], "skipped": [
        {"sheet": "BOM", "rows": [[1, 1]], "reason": "title", "rationale": "Title block above the header."},
        {"sheet": "BOM", "rows": [[2, 2]], "reason": "header", "rationale": "Column headings."},
        {"sheet": "BOM", "rows": [[6, 7]], "reason": "total", "rationale": "Blank spacer and the parts total."},
    ], "ignored_columns": [{"sheet": "BOM", "column": 1, "reason": "Find numbers; line identity is the part number."}]}
    return source, out, row_map


def run(source, out, row_map):
    return verify_row_map(source, ".xlsx", out, json.dumps(row_map).encode())


def test_every_source_row_accounted_for_passes_with_counts_only():
    result = run(*good())
    assert result["valid"] is True
    assert (result["source_rows"], result["line_rows"], result["skipped_rows"], result["output_lines"]) == (6, 3, 3, 2)
    assert result["failed_rows"] == [] and result["unmapped_columns"] == []
    assert "R-100" not in json.dumps(result)  # counts and locations only, never values


def test_a_dropped_source_row_is_reported_with_its_location():
    source, out, row_map = good()
    row_map["lines"][0]["rows"] = [3]
    del row_map["lines"][0]["fields"]["manufacturer_2"]
    del row_map["lines"][0]["fields"]["manufacturer_item_number_2"]
    result = run(source, out, row_map)
    assert result["valid"] is False
    assert {"sheet": "BOM", "row": 4, "code": "ROW_UNACCOUNTED"} in result["failed_rows"]


def test_a_component_row_hidden_as_skipped_is_caught():
    source, out, row_map = good()
    # Drop line 2 from the output and claim its row is a note.
    out = output([{"item_number": "R-100", "item_name": "Resistor 10k", "quantity": 4, "bom_notes": "RoHS",
                   "manufacturer_1": "Acme", "manufacturer_item_number_1": "RC0603",
                   "manufacturer_2": "Beta", "manufacturer_item_number_2": "RB0603"}])
    row_map["lines"] = row_map["lines"][:1]
    row_map["skipped"].append({"sheet": "BOM", "rows": [[5, 5]], "reason": "note", "rationale": "Claimed note."})
    result = run(source, out, row_map)
    assert {"sheet": "BOM", "row": 5, "code": "SKIPPED_ROW_LOOKS_LIKE_LINE"} in result["failed_rows"]


def test_an_invented_or_changed_value_fails_on_its_row():
    source, _, row_map = good()
    out = output([
        {"item_number": "R-100", "item_name": "Resistor 10k", "quantity": 40, "bom_notes": "RoHS",
         "manufacturer_1": "Acme", "manufacturer_item_number_1": "RC0603",
         "manufacturer_2": "Beta", "manufacturer_item_number_2": "RB0603"},
        {"item_number": "C-200", "item_name": "Capacitor 1uF", "quantity": 2,
         "manufacturer_1": "Gamma", "manufacturer_item_number_1": "GC0805"},
    ])
    result = run(source, out, row_map)
    assert {"sheet": "BOM", "row": 3, "code": "FIELD_VALUE_MISMATCH"} in result["failed_rows"]


def test_an_output_value_without_a_source_link_fails():
    source, out, row_map = good()
    del row_map["lines"][1]["fields"]["manufacturer_1"]
    result = run(source, out, row_map)
    assert result["failure_codes"].get("FIELD_NOT_LINKED") == 1


def test_a_row_in_two_lines_and_a_row_both_used_and_skipped_fail():
    source, out, row_map = good()
    twice = copy.deepcopy(row_map)
    twice["lines"][1]["rows"] = [4, 5]
    assert "ROW_IN_TWO_LINES" in run(source, out, twice)["failure_codes"]
    both = copy.deepcopy(row_map)
    both["skipped"].append({"sheet": "BOM", "rows": [[4, 4]], "reason": "note", "rationale": "Overlap."})
    assert "ROW_IN_LINE_AND_SKIPPED" in run(source, out, both)["failure_codes"]


def test_a_pair_split_across_rows_fails_even_when_values_match():
    # Both rows carry MPN "A-1", so a cross-row citation matches by value; the pair check catches it.
    source = xlsx([["Part", "Desc", "Qty", "MFG", "MPN"], ["P-1", "Widget", 1, "Acme", "A-1"],
                   [None, None, None, "Beta", "A-1"]])
    out = output([{"item_number": "P-1", "item_name": "Widget", "quantity": 1,
                   "manufacturer_1": "Acme", "manufacturer_item_number_1": "A-1",
                   "manufacturer_2": "Beta", "manufacturer_item_number_2": "A-1"}])
    fields = {"item_number": [2, 1], "item_name": [2, 2], "quantity": [2, 3],
              "manufacturer_1": [2, 4], "manufacturer_item_number_1": [2, 5],
              "manufacturer_2": [3, 4], "manufacturer_item_number_2": [3, 5]}
    row_map = {"version": VERSION, "lines": [{"output_row": 2, "sheet": "BOM", "rows": [2, 3], "fields": fields}],
               "skipped": [{"sheet": "BOM", "rows": [[1, 1]], "reason": "header", "rationale": "Headings."}],
               "ignored_columns": []}
    assert run(source, out, row_map)["valid"] is True
    fields["manufacturer_item_number_2"] = [2, 5]
    assert run(source, out, row_map)["failed_rows"] == [{"sheet": "BOM", "row": 2, "code": "PAIR_ROWS_DIFFER"}]


def test_an_unmapped_column_with_data_in_bom_lines_fails():
    source, out, row_map = good()
    row_map["ignored_columns"] = []
    result = run(source, out, row_map)
    assert result["valid"] is False and result["unmapped_columns"] == [{"sheet": "BOM", "column": 1}]


def test_a_derived_value_is_allowed_only_for_unit_and_notes():
    source = xlsx([["Part", "Desc", "Qty"], ["P-1", "Widget", 3]])
    out = output([{"item_number": "P-1", "item_name": "Widget", "quantity": 3, "unit_of_measure": "EA"}])
    row_map = {"version": VERSION, "lines": [{"output_row": 2, "sheet": "BOM", "rows": [2], "fields": {
        "item_number": [2, 1], "item_name": [2, 2], "quantity": [2, 3],
        "unit_of_measure": {"derived": "No unit column; every line is counted in each."}}}],
        "skipped": [{"sheet": "BOM", "rows": [[1, 1]], "reason": "header", "rationale": "Headings."}],
        "ignored_columns": []}
    assert run(source, out, row_map)["valid"] is True
    row_map["lines"][0]["fields"]["item_name"] = {"derived": "Made up."}
    assert "FIELD_DERIVATION_NOT_ALLOWED" in run(source, out, row_map)["failure_codes"]


def test_one_part_of_a_combined_cell_may_be_cited():
    source = xlsx([["Part", "Desc", "Qty", "MFG", "MPN"], ["P-1", "Widget", 1, "Acme; Beta", "A-1; B-2"]])
    out = output([{"item_number": "P-1", "item_name": "Widget", "quantity": 1,
                   "manufacturer_1": "Acme", "manufacturer_item_number_1": "A-1",
                   "manufacturer_2": "Beta", "manufacturer_item_number_2": "B-2"}])
    fields = {"item_number": [2, 1], "item_name": [2, 2], "quantity": [2, 3],
              "manufacturer_1": [2, 4, "part"], "manufacturer_item_number_1": [2, 5, "part"],
              "manufacturer_2": [2, 4, "part"], "manufacturer_item_number_2": [2, 5, "part"]}
    row_map = {"version": VERSION, "lines": [{"output_row": 2, "sheet": "BOM", "rows": [2], "fields": fields}],
               "skipped": [{"sheet": "BOM", "rows": [[1, 1]], "reason": "header", "rationale": "Headings."}],
               "ignored_columns": []}
    assert run(source, out, row_map)["valid"] is True
    fields["manufacturer_item_number_2"] = [2, 5]  # whole cell is "A-1; B-2", not "B-2"
    assert "FIELD_VALUE_MISMATCH" in run(source, out, row_map)["failure_codes"]


def test_a_wide_output_with_twenty_pairs_is_accounted_for():
    header = ["Part", "Desc", "Qty"] + [h for i in range(1, 21) for h in (f"MFG{i}", f"MPN{i}")]
    values = ["P-1", "Widget", 2] + [v for i in range(1, 21) for v in (f"Maker{i}", f"PN-{i}")]
    source = xlsx([header, values])
    line = {"item_number": "P-1", "item_name": "Widget", "quantity": 2}
    fields = {"item_number": [2, 1], "item_name": [2, 2], "quantity": [2, 3]}
    for i in range(1, 21):
        line[f"manufacturer_{i}"] = f"Maker{i}"; line[f"manufacturer_item_number_{i}"] = f"PN-{i}"
        fields[f"manufacturer_{i}"] = [2, 2 + 2 * i]; fields[f"manufacturer_item_number_{i}"] = [2, 3 + 2 * i]
    row_map = {"version": VERSION, "lines": [{"output_row": 2, "sheet": "BOM", "rows": [2], "fields": fields}],
               "skipped": [{"sheet": "BOM", "rows": [[1, 1]], "reason": "header", "rationale": "Headings."}],
               "ignored_columns": []}
    assert run(source, output([line], pairs=20), row_map)["valid"] is True


@pytest.mark.parametrize("mutate, code", [
    (lambda m: m.update(version="bom-row-map.v0"), "ROW_MAP_VERSION_INVALID"),
    (lambda m: m.pop("ignored_columns"), "ROW_MAP_KEYS_INVALID"),
    (lambda m: m["lines"][0].update(output_row=99), "ROW_MAP_OUTPUT_ROW_INVALID"),
    (lambda m: m["lines"].pop(), "ROW_MAP_OUTPUT_COVERAGE_INVALID"),
    (lambda m: m["skipped"][0].update(reason="because"), "ROW_MAP_SKIP_INVALID"),
    (lambda m: m["lines"][0]["fields"].update(line_number=[3, 1]), "ROW_MAP_FIELDS_INVALID"),
    (lambda m: m["lines"][0]["fields"].update(item_name=[3, 3, "all"]), "ROW_MAP_LINK_INVALID"),
])
def test_a_malformed_row_map_is_refused_with_a_stable_code(mutate, code):
    source, out, row_map = good()
    mutate(row_map)
    with pytest.raises(RowMapError, match=code):
        run(source, out, row_map)


def test_duplicate_json_keys_and_nan_are_refused():
    source, out, _ = good()
    with pytest.raises(RowMapError, match="ROW_MAP_DUPLICATE_KEY"):
        verify_row_map(source, ".xlsx", out, b'{"version":"a","version":"b"}')
    with pytest.raises(RowMapError, match="ROW_MAP_JSON_INVALID"):
        verify_row_map(source, ".xlsx", out, b'{"version":NaN}')
