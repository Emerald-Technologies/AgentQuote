import pytest

from bom_processor.semantic import AssemblyIdentity, compare_boms, normalize_bom

A = AssemblyIdentity("Board 01", "Rev A")

def row(**changes):
    value = {"item_number": "R1", "revision": "1", "item_category": "Part", "quantity": 2,
             "reference_designator": "R2, R1", "manufacturer_1": "Acme", "manufacturer_item_number_1": "100"}
    value.update(changes)
    return value


def test_order_format_and_case_are_equivalent():
    left = [row()]
    right = [row(item_number=" R1 ", item_category="part", quantity="2.0", reference_designator="r1 r2")]
    result = compare_boms(left, right, left_assembly=A, right_assembly=A)
    assert result.relation == "equivalent"
    assert result.left_fingerprint == result.right_fingerprint


def test_row_order_does_not_change_fingerprint():
    first = [row(item_number="R1"), row(item_number="C1", reference_designator="C1")]
    second = [first[1], first[0]]
    assert normalize_bom(first, A).fingerprint == normalize_bom(second, A).fingerprint


def test_more_mpn_alternatives_dominates_without_conflict():
    result = compare_boms(
        [row()],
        [row(manufacturer_2="Other", manufacturer_item_number_2="200")],
        left_assembly=A, right_assembly=A,
    )
    assert result.relation == "dominates"
    assert result.evidence[0]["side"] == "right"


def test_disjoint_mpn_alternatives_are_non_conflicting_enrichment():
    result = compare_boms(
        [row(manufacturer_1="Acme", manufacturer_item_number_1="100")],
        [row(manufacturer_1="Other", manufacturer_item_number_1="200")],
        left_assembly=A, right_assembly=A,
    )
    assert result.relation == "equivalent"  # neither side is a strict richer superset


@pytest.mark.parametrize("changes", [
    {"quantity": 3}, {"reference_designator": "R3"}, {"item_category": "Assembly"},
])
def test_material_fact_changes_conflict(changes):
    result = compare_boms([row()], [row(**changes)], left_assembly=A, right_assembly=A)
    assert result.relation == "conflict"
    assert result.evidence[0]["kind"] == "material_conflict"


def test_missing_component_conflicts():
    result = compare_boms([row()], [], left_assembly=A, right_assembly=A)
    assert result.relation == "conflict"
    assert result.evidence[0]["kind"] == "component_added_or_missing"


def test_assembly_revision_mismatch_conflicts():
    result = compare_boms([row()], [row()], left_assembly=A, right_assembly=AssemblyIdentity("Board 01", "Rev B"))
    assert result.relation == "conflict"
    assert result.evidence[0]["kind"] == "assembly_revision"


def test_description_is_non_material():
    result = compare_boms([row(description="old")], [row(description="new")], left_assembly=A, right_assembly=A)
    assert result.relation == "equivalent"
