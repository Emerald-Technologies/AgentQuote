"""Document evidence has no priced quantity; part validation remains strict."""
import io

import pandas as pd
import pytest

from bom_processor.core import CANONICAL_COLUMNS, ProcessingError
from bom_processor.remote import _validate_xlsx_bytes
from bom_processor.candidate_validation import _semantic_output
from bom_processor.semantic import AssemblyIdentity, compare_boms
from test_arena_policy import arena, output, parsed, validate_fixture, verify_evidence


def workbook(category, quantity):
    row = dict.fromkeys(CANONICAL_COLUMNS)
    row.update(line_number=1, item_number="ASM", item_name="Specification",
               revision="A", item_category=category, quantity=quantity)
    stream = io.BytesIO()
    pd.DataFrame([row], columns=CANONICAL_COLUMNS).to_excel(stream, index=False)
    return stream.getvalue()


@pytest.mark.parametrize("quantity", [None, 0, 1, 2.5, -1])
def test_document_blank_or_numeric_quantity_validates_and_is_preserved(quantity):
    data = workbook("Document", quantity)
    _validate_xlsx_bytes(data, 100)
    result = _semantic_output(data, data, "xlsx")
    assert result["valid"] is True
    value = pd.read_excel(io.BytesIO(data)).iloc[0]["quantity"]
    assert pd.isna(value) if quantity is None else value == quantity


@pytest.mark.parametrize("quantity", [None, "bad", float("inf")])
def test_part_missing_or_invalid_quantity_still_fails(quantity):
    with pytest.raises(ProcessingError, match="quantity invalid"):
        _validate_xlsx_bytes(workbook("Part", quantity), 100)


@pytest.mark.parametrize("quantity", [None, 0, -1])
def test_part_semantic_positive_quantity_check_unchanged(quantity):
    data = workbook("Part", quantity)
    assert _semantic_output(data, data, "xlsx")["valid"] is False


def test_document_quantity_is_not_material_but_revision_and_part_quantity_are():
    identity = AssemblyIdentity("ASM", "A")
    doc = {"item_number": "ASM", "revision": "A", "item_category": "Document", "quantity": None}
    changed = {**doc, "quantity": 99}
    result = compare_boms([doc], [changed], left_assembly=identity, right_assembly=identity)
    assert result.relation == "equivalent"
    assert result.left_fingerprint == result.right_fingerprint
    assert compare_boms([doc], [{**changed, "revision": "B"}],
                        left_assembly=identity, right_assembly=identity).relation == "conflict"
    part = {**doc, "item_category": "Part", "quantity": 1}
    assert compare_boms([part], [{**part, "quantity": 2}],
                        left_assembly=identity, right_assembly=identity).relation == "conflict"


@pytest.mark.parametrize("category", ["Document", "Drawing", "Procedure", "Specification"])
@pytest.mark.parametrize("quantity", ["", "0", "3"])
def test_arena_preserves_document_rows_regardless_of_quantity(category, quantity):
    source = arena([["DOC", "Specification", "A", category, "EA", quantity]])
    row = parsed("DOC", None if not quantity else int(quantity), "", "")
    row["item_category"] = "Document"
    evidence = validate_fixture(source, output([row]))
    assert evidence["valid"] is True
    assert evidence["omitted_count"] == 0
    assert verify_evidence(evidence)
    assert validate_fixture(source, output([]))["reason"] == "identity_coverage"
    row["item_category"] = "Part"
    assert validate_fixture(source, output([row]))["reason"] == "document_category_mismatch"


@pytest.mark.parametrize("source_quantity, changed", [("", 1), ("3", None), ("3", 99)])
def test_arena_rejects_invented_or_discarded_document_quantity(source_quantity, changed):
    source = arena([["DOC", "Specification", "A", "Document", "EA", source_quantity]])
    row = parsed("DOC", changed, "", "")
    row["item_category"] = "Document"
    assert validate_fixture(source, output([row]))["reason"] == "document_quantity_changed"