"""PLM-export readiness, unit 1 (Nick 2026-10-12): maker codes in front of names (D1), skipped rows
that hold a manufacturer or MPN (D3) and the Gov-only model endpoint (D5). Synthetic data only."""
import io
import json

import pytest
from openpyxl import Workbook

from bom_processor.canonical_format import canonical_columns
from bom_processor.gov_endpoint import is_gov_endpoint
from bom_processor.row_accounting import VERSION, verify_row_map

HEADER = ["Part", "Desc", "Qty", "Approved Manufacturer(s)", "Manufacturer P/N"]


def xlsx(rows, title="BOM"):
    book = Workbook(); sheet = book.active; sheet.title = title
    for row in rows:
        sheet.append(row)
    data = io.BytesIO(); book.save(data); book.close()
    return data.getvalue()


def output(lines):
    columns = canonical_columns(8)
    rows = [columns]
    for number, values in enumerate(lines, 1):
        row = dict.fromkeys(columns)
        row.update(line_number=number, item_category="Part", **values)
        rows.append([row[column] for column in columns])
    return xlsx(rows, title="Parsed")


def run(source, out, row_map):
    return verify_row_map(source, ".xlsx", out, json.dumps(row_map).encode())


def plm(maker_out="MAKER ALPHA INC.", maker_link=(2, 4, "code")):
    """One line whose alternates sit on the rows below, makers carrying a customer code."""
    source = xlsx([HEADER,
                   ["P-1", "Regulator", 1, "63 MAKER ALPHA INC.", "ZX3010-5"],
                   [None, None, None, "63 MAKER ALPHA INC.", "ZX3010-5#PBF"],
                   [None, None, None, "117 MAKER BETA CORP", "BT3010"]])
    out = output([{"item_number": "P-1", "item_name": "Regulator", "quantity": 1,
                   "manufacturer_1": maker_out, "manufacturer_item_number_1": "ZX3010-5",
                   "manufacturer_2": "MAKER ALPHA INC.", "manufacturer_item_number_2": "ZX3010-5#PBF",
                   "manufacturer_3": "MAKER BETA CORP", "manufacturer_item_number_3": "BT3010"}])
    fields = {"item_number": [2, 1], "item_name": [2, 2], "quantity": [2, 3],
              "manufacturer_1": list(maker_link), "manufacturer_item_number_1": [2, 5],
              "manufacturer_2": [3, 4, "code"], "manufacturer_item_number_2": [3, 5],
              "manufacturer_3": [4, 4, "code"], "manufacturer_item_number_3": [4, 5]}
    row_map = {"version": VERSION, "lines": [{"output_row": 2, "sheet": "BOM", "rows": [2, 3, 4], "fields": fields}],
               "skipped": [{"sheet": "BOM", "rows": [[1, 1]], "reason": "header", "rationale": "Headings."}],
               "ignored_columns": []}
    return source, out, row_map


def test_a_maker_written_without_its_customer_code_is_proven_by_a_code_link():
    result = run(*plm())
    assert result["valid"] is True, result["failed_rows"]
    assert result["maker_code_prefix_kept"] == 0
    assert "MAKER" not in json.dumps(result)  # counts and locations only


def test_a_code_link_must_match_the_rest_of_the_cell_exactly():
    result = run(*plm(maker_out="MAKER ALPHA"))
    assert {"sheet": "BOM", "row": 2, "code": "FIELD_VALUE_MISMATCH"} in result["failed_rows"]


def test_a_code_link_needs_a_code_in_the_cell():
    source = xlsx([HEADER, ["P-1", "Tape", 1, "3M", "TP-1"]])
    out = output([{"item_number": "P-1", "item_name": "Tape", "quantity": 1,
                   "manufacturer_1": "M", "manufacturer_item_number_1": "TP-1"}])
    row_map = {"version": VERSION, "lines": [{"output_row": 2, "sheet": "BOM", "rows": [2], "fields": {
        "item_number": [2, 1], "item_name": [2, 2], "quantity": [2, 3],
        "manufacturer_1": [2, 4, "code"], "manufacturer_item_number_1": [2, 5]}}],
        "skipped": [{"sheet": "BOM", "rows": [[1, 1]], "reason": "header", "rationale": "Headings."}],
        "ignored_columns": []}
    assert "FIELD_VALUE_MISMATCH" in run(source, out, row_map)["failure_codes"]


def test_a_code_link_is_only_for_manufacturer_names():
    source, out, row_map = plm()
    row_map["lines"][0]["fields"]["manufacturer_item_number_1"] = [2, 5, "code"]
    assert "CODE_LINK_NOT_ALLOWED" in run(source, out, row_map)["failure_codes"]


def test_a_maker_kept_with_its_code_is_valid_and_counted():
    result = run(*plm(maker_out="63 MAKER ALPHA INC.", maker_link=(2, 4)))
    assert result["valid"] is True and result["maker_code_prefix_kept"] == 1


def test_alternate_rows_skipped_as_notes_fail_instead_of_losing_pairs():
    source, _, row_map = plm()
    out = output([{"item_number": "P-1", "item_name": "Regulator", "quantity": 1,
                   "manufacturer_1": "MAKER ALPHA INC.", "manufacturer_item_number_1": "ZX3010-5"}])
    line = row_map["lines"][0]
    line["rows"] = [2]
    line["fields"] = {k: v for k, v in line["fields"].items() if k.endswith("_1") or not k.startswith("manufacturer")}
    row_map["skipped"].append({"sheet": "BOM", "rows": [[3, 4]], "reason": "note", "rationale": "Continuation."})
    result = run(source, out, row_map)
    assert result["valid"] is False
    assert {"sheet": "BOM", "row": 3, "code": "SKIPPED_ROW_HOLDS_PAIR"} in result["failed_rows"]
    assert {"sheet": "BOM", "row": 4, "code": "SKIPPED_ROW_HOLDS_PAIR"} in result["failed_rows"]


def test_a_skipped_row_with_only_an_mpn_fails_and_header_rows_are_exempt():
    source = xlsx([HEADER, ["P-1", "Cap", 2, "MAKER", "C-1"], [None, None, None, None, "C-1#TR"],
                   HEADER])  # a repeated header further down, as page breaks in exports give
    out = output([{"item_number": "P-1", "item_name": "Cap", "quantity": 2,
                   "manufacturer_1": "MAKER", "manufacturer_item_number_1": "C-1"}])
    row_map = {"version": VERSION, "lines": [{"output_row": 2, "sheet": "BOM", "rows": [2], "fields": {
        "item_number": [2, 1], "item_name": [2, 2], "quantity": [2, 3],
        "manufacturer_1": [2, 4], "manufacturer_item_number_1": [2, 5]}}],
        "skipped": [{"sheet": "BOM", "rows": [[1, 1], [4, 4]], "reason": "header", "rationale": "Headings."},
                    {"sheet": "BOM", "rows": [[3, 3]], "reason": "note", "rationale": "Packaging note."}],
        "ignored_columns": []}
    result = run(source, out, row_map)
    assert result["failed_rows"] == [{"sheet": "BOM", "row": 3, "code": "SKIPPED_ROW_HOLDS_PAIR"}]


def test_only_rows_shaped_like_alternates_are_held_when_skipped():
    # Columns: part, desc, qty, maker, MPN, rank (rank is ignored). Row 3 is an alternate with its
    # rank filled; row 5 is a heading row labelled title; row 6 a document row whose ID sits in the
    # maker column (overloaded columns) with a description.
    source = xlsx([HEADER + ["Rank"], ["P-1", "Cap", 2, "MAKER", "C-1", 1], [None, None, None, "MAKER", "C-1#TR", 2],
                   ["P-2", "Res", 4, "MAKER", "R-1", 1], ["Part", "Desc", "Qty", "Maker", "Maker P/N", "Rank"],
                   [None, "Assembly drawing", None, "DWG-100", "B", None]])
    out = output([{"item_number": "P-1", "item_name": "Cap", "quantity": 2,
                   "manufacturer_1": "MAKER", "manufacturer_item_number_1": "C-1"},
                  {"item_number": "P-2", "item_name": "Res", "quantity": 4,
                   "manufacturer_1": "MAKER", "manufacturer_item_number_1": "R-1"}])
    fields = lambda r: {"item_number": [r, 1], "item_name": [r, 2], "quantity": [r, 3],
                        "manufacturer_1": [r, 4], "manufacturer_item_number_1": [r, 5]}
    row_map = {"version": VERSION, "lines": [{"output_row": 2, "sheet": "BOM", "rows": [2], "fields": fields(2)},
                                             {"output_row": 3, "sheet": "BOM", "rows": [4], "fields": fields(4)}],
               "skipped": [{"sheet": "BOM", "rows": [[1, 1]], "reason": "header", "rationale": "Headings."},
                           {"sheet": "BOM", "rows": [[3, 3]], "reason": "note", "rationale": "Packaging."},
                           {"sheet": "BOM", "rows": [[5, 5]], "reason": "title", "rationale": "Repeated title."},
                           {"sheet": "BOM", "rows": [[6, 6]], "reason": "note", "rationale": "Drawing reference."}],
               "ignored_columns": [{"sheet": "BOM", "column": 6, "reason": "Alternate rank."}]}
    assert run(source, out, row_map)["failed_rows"] == [{"sheet": "BOM", "row": 3, "code": "SKIPPED_ROW_HOLDS_PAIR"}]


@pytest.mark.parametrize("endpoint", ["https://acct.services.ai.azure.us/api/projects/p",
                                      "https://acct.services.ai.azure.us:443/api/projects/p"])
def test_gov_endpoints_are_accepted(endpoint):
    assert is_gov_endpoint(endpoint) is True


@pytest.mark.parametrize("endpoint", [None, "", "http://example.azure.us/p", "https://example.azure.com/p",
    "https://acct.services.ai.azure.com/api/projects/p", "https://example.azure.us.evil.test/p",
    "https://user:pass@example.azure.us/p", "https://example.azure.us/p?token=x",
    "https://example.azure.us:444/p", "https://example.azure.us/p#x", "https://example.azure.us/p q",
    "https://example.azure.us:99999/p"])
def test_other_endpoints_are_refused(endpoint):
    assert is_gov_endpoint(endpoint) is False


@pytest.mark.parametrize("endpoint", ["", "https://acct.services.ai.azure.com/api/projects/p"])
def test_parser_authoring_refuses_a_non_gov_endpoint_before_any_client(monkeypatch, endpoint):
    from bom_processor.foundry_author import create_foundry_author
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", endpoint)
    with pytest.raises(ValueError, match="MODEL_ENDPOINT_INVALID"):
        create_foundry_author()


def test_authoring_rules_and_guidance_carry_the_maker_code_clue():
    from bom_processor.candidate_authoring import CANONICAL_OUTPUT_RULES, ROW_MAP_RULE
    from bom_processor.document_role import BOM_READING_GUIDANCE
    from bom_processor.row_refinement import refinement_instructions
    rules = " ".join(CANONICAL_OUTPUT_RULES)
    assert "customer's own code" in rules and "3M" in rules and "clue, not a rule" in rules
    assert '[row, column, \\"code\\"]' in json.dumps(ROW_MAP_RULE)
    assert "customer's own code" in BOM_READING_GUIDANCE and "3M" in BOM_READING_GUIDANCE
    text = refinement_instructions({"rows": []}, 1, 2)
    assert "SKIPPED_ROW_HOLDS_PAIR" in text and "CODE_LINK_NOT_ALLOWED" in text
