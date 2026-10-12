"""Round 11 (A043, Nick 2026-10-12): feedback names what to fix.

- FIELD_NOT_LINKED records which output field a line wrote without a link; the rewrite's excerpt and the
  plain feedback both name it (A043 ENE: 34 rows failed and the rewrite saw only the row numbers).
- A long crash message keeps its final error line (A043 ENE: the 500-character cut kept the format hint
  and the traceback header and dropped the actual error, twice).
Run: python -m pytest tests/test_refinement_feedback_detail.py
"""
import hashlib
import json

from bom_processor.candidate_validation import CandidateValidationService, _row_feedback, _row_fields
from bom_processor.row_refinement import failed_row_excerpt, refinement_instructions
from test_row_accounting import good, run


def test_an_unlinked_field_is_named_by_row_and_field():
    source, out, row_map = good()
    del row_map["lines"][1]["fields"]["manufacturer_1"]
    result = run(source, out, row_map)
    assert result["failure_codes"].get("FIELD_NOT_LINKED") == 1
    assert {"sheet": "BOM", "row": result["failed_rows"][0]["row"], "field": "manufacturer_1"} in result["unlinked_fields"]
    feedback = _row_feedback(result)
    assert "output fields written without a link" in feedback and "manufacturer_1" in feedback
    fields = _row_fields({"row_accounting": result})
    assert any(item.get("unlinked") and item["field"] == "manufacturer_1" for item in fields)
    failure = {"failed_row_count": result["failed_row_count"], "failed_rows": result["failed_rows"],
               "source_sha256": hashlib.sha256(source).hexdigest(), "unmapped_columns": [], "pattern_findings": []}
    excerpt = failed_row_excerpt(source, ".xlsx", failure, fields)
    row = next(entry for entry in excerpt["rows"] if entry["code"] == "FIELD_NOT_LINKED")
    assert row["unlinked"] == ["manufacturer_1"]
    assert "unlinked lists the output fields" in refinement_instructions(excerpt, 1, 2)


def test_a_long_crash_message_keeps_its_final_error_line():
    message = ("parser failed | input is a ZIP-based Office file (.xlsx); use openpyxl Traceback (most recent call last):\n"
               + "  File \"/tmp/parse.py\", line 12, in helper\n    value = table[key]\n" * 30
               + "KeyError: 'value'")
    raw = json.dumps({"failure_classification": {"error_type": "RuntimeError", "error_message": message}}).encode()
    result = CandidateValidationService._extract_failure_classification(raw)
    assert len(result["error_message"]) <= 500
    assert result["error_message"].startswith("KeyError: 'value' | ")
    assert result["error_message"].endswith("KeyError: 'value'")


def test_a_short_crash_message_is_unchanged():
    raw = json.dumps({"failure_classification": {"error_type": "RuntimeError",
                                                 "error_message": "parser failed | ValueError: no BOM table"}}).encode()
    assert CandidateValidationService._extract_failure_classification(raw)["error_message"] == "parser failed | ValueError: no BOM table"


def test_one_maker_with_several_part_numbers_in_one_cell_is_valid():
    # Round 11 (A043 row 81): "NICHTEK" with "PDCQ580JB-SMB, PDCQ580JU-SMB" is two pairs from one maker.
    from test_row_accounting import VERSION, output, xlsx
    source = xlsx([["Part", "Qty", "MFG", "MPN"], ["D-1", 1, "NICHTEK", "PDCQ580JB-SMB, PDCQ580JU-SMB"]])
    out = output([{"item_number": "D-1", "item_name": "D-1", "quantity": 1,
                   "manufacturer_1": "NICHTEK", "manufacturer_item_number_1": "PDCQ580JB-SMB",
                   "manufacturer_2": "NICHTEK", "manufacturer_item_number_2": "PDCQ580JU-SMB"}])
    fields = {"item_number": [2, 1], "item_name": [2, 1], "quantity": [2, 2],
              "manufacturer_1": [2, 3, "part"], "manufacturer_item_number_1": [2, 4, "part"],
              "manufacturer_2": [2, 3, "part"], "manufacturer_item_number_2": [2, 4, "part"]}
    row_map = {"version": VERSION, "lines": [{"output_row": 2, "sheet": "BOM", "rows": [2], "fields": fields}],
               "skipped": [{"sheet": "BOM", "rows": [[1, 1]], "reason": "header", "rationale": "Column headings."}],
               "ignored_columns": []}
    assert run(source, out, row_map)["valid"] is True
    # Dropping one of the maker's part numbers is still found.
    out = output([{"item_number": "D-1", "item_name": "D-1", "quantity": 1,
                   "manufacturer_1": "NICHTEK", "manufacturer_item_number_1": "PDCQ580JB-SMB"}])
    for key in ("manufacturer_2", "manufacturer_item_number_2"):
        del fields[key]
    result = run(source, out, row_map)
    assert {"sheet": "BOM", "row": 2, "code": "COMBINED_PART_NOT_CARRIED"} in result["failed_rows"]


def test_rules_say_separators_are_clues_and_one_maker_can_have_several_part_numbers():
    from bom_processor.candidate_authoring import CANONICAL_OUTPUT_RULES
    rules = " ".join(CANONICAL_OUTPUT_RULES)
    assert "one pair per part number, each with that manufacturer" in rules and "BAS21,235" in rules
