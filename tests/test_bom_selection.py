"""Content-based BOM alignment and primary selection (synthetic, offline)."""
import pytest

from bom_processor.bom_selection import align_boms, attribute_undeclared, select_primary


def line(item, refdes=None, qty=None, mpn=None, category="Part", **extra):
    row = {"item_number": item, "reference_designator": refdes, "quantity": qty,
           "manufacturer_item_number_1": mpn, "item_category": category}
    row.update(extra)
    return row


PLAIN = [line("1000-0001", "C1,C2", 2, "GRM1"), line("1000-0002", "R1-R3", 3, "RC0402"),
         line("1000-0003", "U1", 1, "STM32"), line("1000-0099", None, 1, "PCB-A")]
# Same BOM with a fixed 3-character item prefix, a parent row, one extra placement
# and procurement data: the MOD layout in the reference package.
MOD = [line("ASY-0001", None, 1, None, procurement="make"),
       line("ENE1000-0001", "C2,C1", 2, "GRM1", procurement="buy"),
       line("ENE1000-0002", "R1,R2,R3", 3, "RC0402", procurement="buy"),
       line("ENE1000-0003", "U1", 1, "STM32", procurement="buy"),
       line("ENE1000-0004", "D1", 1, "LED1", procurement="buy"),
       line("ENE1000-0099", None, 1, "PCB-A", procurement="buy")]


def test_prefixed_superset_is_detected_from_data():
    result = align_boms(MOD, PLAIN)
    assert result["relation"] == "superset"
    assert result["item_number_relation"] == {"kind": "prefix", "carried_by": "left", "affix": "ene"}
    assert result["matched_by"] == {"manufacturer_part_number": 4}
    assert result["right_only_lines"] == 0 and result["left_only_lines"] == 2
    assert result["refdes_compared_lines"] == 3 and result["refdes_agreeing_lines"] == 3
    assert align_boms(PLAIN, MOD)["relation"] == "subset"


def test_reference_designators_are_never_required():
    no_refdes = [dict(row, reference_designator=None) for row in PLAIN]
    result = select_primary({"plain": no_refdes, "mod": MOD})
    assert result["primary"] == "mod" and result["roles"]["plain"] == "redundant"
    assert result["comparisons"][0]["refdes_compared_lines"] == 0
    both_without = [dict(row, reference_designator=None) for row in MOD]
    assert align_boms(both_without, no_refdes)["relation"] == "superset"


def test_disagreeing_reference_designators_are_not_a_conflict():
    regrouped = [dict(row) for row in PLAIN]
    regrouped[0]["reference_designator"] = "C1 C2 C3"
    result = align_boms(MOD, regrouped)
    assert result["relation"] == "superset"
    assert result["refdes_compared_lines"] == 3 and result["refdes_agreeing_lines"] == 2


def test_item_number_matches_lines_without_part_numbers():
    # Part-numbered lines prove the prefix; it then matches lines with no part number.
    plain = PLAIN + [line("1000-0007", None, 4, None)]
    mod = MOD + [line("ENE1000-0007", None, 4, None)]
    result = align_boms(mod, plain)
    assert result["relation"] == "superset"
    assert result["matched_by"] == {"item_number": 1, "manufacturer_part_number": 4}


def test_primary_is_the_bom_that_contains_the_other():
    result = select_primary({"plain": PLAIN, "mod": MOD})
    assert result["primary"] == "mod"
    assert result["roles"] == {"mod": "primary", "plain": "redundant"}
    assert result["reason"] == "contains_all_others"


def test_identical_boms_keep_the_more_complete_one():
    sparse = [dict(row, procurement=None) for row in MOD]
    result = select_primary({"a": sparse, "b": MOD})
    assert result["primary"] == "b" and result["roles"]["a"] == "identical"


@pytest.mark.parametrize("change, attribute", [
    (lambda rows: rows[2].update(quantity=4), "quantity"),
    (lambda rows: rows[3].update(manufacturer_item_number_1="OTHER"), "manufacturer_part_number"),
])
def test_true_conflict_never_selects(change, attribute):
    rows = [dict(row) for row in PLAIN]
    change(rows)
    result = select_primary({"plain": rows, "mod": MOD})
    assert result["primary"] is None and result["reason"] == "conflict"
    assert result["comparisons"][0]["conflict_examples"][0]["attribute"] == attribute


def test_neither_contains_the_other_needs_review():
    other = PLAIN + [line("1000-0050", "Q9", 1, "BSS138")]
    result = select_primary({"plain": other, "mod": MOD})
    assert result["primary"] is None and result["reason"] == "no_bom_contains_all_others"


def test_mixed_item_relations_are_not_learned():
    rows = [dict(row) for row in PLAIN]
    rows[1]["item_number"] = "XYZ-1"
    result = align_boms(MOD, rows)
    assert result["item_number_relation"] is None
    assert result["item_number_relation_basis"] == "not_proven"
    # Part-number matches still stand; an unproven relation never matches extra lines.
    assert result["matched_by"] == {"manufacturer_part_number": 4}


def test_without_any_part_numbers_only_identical_item_numbers_match():
    a = [line("P-1", None, 1), line("P-2", None, 2)]
    b = [line("P-1", None, 1)]
    result = align_boms(a, b)
    assert result["relation"] == "superset" and result["item_number_relation_basis"] == "identical_only"
    assert align_boms(a, [line("X-P-1", None, 1)])["relation"] == "overlap"


def test_undeclared_bom_attributed_to_the_single_containing_assembly():
    other_assembly = [line("2000-0001", "C1", 1, "X1"), line("2000-0002", "R9", 1, "X2")]
    result = attribute_undeclared(PLAIN, {"asm-a": MOD, "asm-b": other_assembly})
    assert result["assembly"] == "asm-a" and result["reason"] == "contained_in_one_assembly"
    assert attribute_undeclared(PLAIN, {"asm-b": other_assembly})["assembly"] is None
    assert attribute_undeclared(PLAIN, {"a": MOD, "b": MOD})["reason"] == "contained_in_several_assemblies"
