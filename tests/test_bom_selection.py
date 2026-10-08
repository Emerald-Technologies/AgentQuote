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
    assert result["matched_placed_lines"] == 3 and result["right_only_lines"] == 0
    assert result["left_only_refdes"] == 1 and result["right_only_refdes"] == 0
    assert align_boms(PLAIN, MOD)["relation"] == "subset"


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


def test_regrouped_placements_are_a_conflict_not_a_match():
    regrouped = [line("1000-0001", "C1", 1, "GRM1"), line("1000-0001b", "C2", 1, "GRM1")] + PLAIN[1:]
    assert align_boms(MOD, regrouped)["relation"] == "conflict"


def test_mixed_item_relations_are_not_learned():
    rows = [dict(row) for row in PLAIN]
    rows[1]["item_number"] = "XYZ-1"
    result = align_boms(MOD, rows)
    assert result["item_number_relation"] is None


def test_undeclared_bom_attributed_to_the_single_containing_assembly():
    other_assembly = [line("2000-0001", "C1", 1, "X1"), line("2000-0002", "R9", 1, "X2")]
    result = attribute_undeclared(PLAIN, {"asm-a": MOD, "asm-b": other_assembly})
    assert result["assembly"] == "asm-a" and result["reason"] == "contained_in_one_assembly"
    assert attribute_undeclared(PLAIN, {"asm-b": other_assembly})["assembly"] is None
    assert attribute_undeclared(PLAIN, {"a": MOD, "b": MOD})["reason"] == "contained_in_several_assemblies"
