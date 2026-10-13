"""Roll-down and build/buy proposals (sub-assembly plan unit 3a, Nick 2026-10-12). Synthetic data only."""
import pytest

from bom_processor.bom_structure import Line, resolve


def tree(rows):
    """rows: (part, description, quantity, parent index or None[, category[, has_mpn]])."""
    lines, parents = [], {}
    for index, row in enumerate(rows):
        part, name, qty, up = row[:4]
        category = row[4] if len(row) > 4 else "Part"
        lines.append(Line(index + 2, part, name, category, qty, row[5] if len(row) > 5 else True))
        parents[index + 2] = None if up is None else up + 2
    return lines, parents


def by_part(result):
    return {line["line_key"]: line for line in result["lines"]}


def codes(result):
    return sorted((f["code"], tuple(f["lines"])) for f in result["findings"])


def test_a_flat_bom_is_unchanged():
    lines, parents = tree([("R-1", "RES 10K", 4, None), ("C-1", "CAP 1UF", "2.000", None)])
    result = resolve(lines, parents, proven=True)
    assert [(l["quantity"], l["procurement"], l["bom_level"]) for l in result["lines"]] == [("4", "buy", 1), ("2", "buy", 1)]
    assert result["findings"] == []


def test_a_harness_is_proposed_bought_and_its_children_become_reference_with_rolled_quantities():
    # The synthetic PLM BOM: harness used twice, contact 10 per harness.
    lines, parents = tree([("900-1001-01", "IC, REGULATOR", 1, None),
                           ("900-5000-01", "SUB-ASSY, CONNECTOR HARNESS", 2, None),
                           ("900-5001-01", "CONN, HEADER", 1, 1), ("900-5002-01", "CONTACT, CRIMP", 10, 1)])
    result = resolve(lines, parents, proven=True)
    harness, contact = by_part(result)["900-5000-01"], by_part(result)["900-5000-01/900-5002-01"]
    assert harness["procurement"] == "buy" and harness["procurement_basis"]["reasons"] == ["NAME_CABLE"]
    assert (contact["quantity"], contact["quantity_per_parent"], contact["procurement"]) == ("20", "10", "reference")
    assert codes(result) == [("SUBASSEMBLY_PROPOSED", ("900-5000-01",))]
    # A person decides we build it: children are bought, 2 headers and 20 contacts per assembly.
    built = resolve(lines, parents, proven=True, choices={"900-5000-01": "build"})
    assert by_part(built)["900-5000-01/900-5002-01"]["procurement"] == "buy"
    assert by_part(built)["900-5000-01"]["procurement_basis"]["kind"] == "confirmed"
    assert built["findings"] == []


def test_an_amat_shaped_tree_rolls_down_and_proposes_from_contents():
    lines, parents = tree([
        ("TOP-1", "ASSY, CONTROLLER", "1.000", None),                       # 0 the quoted assembly
        ("ENC-1", "MAIN ENCLOSURE", "1.000", 0), ("PEM-1", "NUT SELF CLINCHING", "4.000", 1),
        ("STK-1", "ASSY, BOARD STACK, HEATSINK", "1.000", 0),                # 3
        ("PCA-1", "PCB ASSY, MAIN", "2.000", 3),                             # 4
        ("FAB-1", "PCB FABRICATION, MAIN", "1.000", 4), ("CAP-1", "CAP CER 10UF", "34.000", 4),
        ("DOC-1", "SCHEMATIC PCB MAIN", "1.000", 4, "Document"),
        ("CBL-1", "CABLE ASSY, FAN", "1.000", 3), ("CON-1", "CONNECTOR", "2.000", 8),
        ("CAP-1", "CAP CER 10UF", "3.000", 0),                               # same part again, top level
    ])
    result = resolve(lines, parents, proven=True, assembly="TOP-1")
    got = by_part(result)
    assert got["TOP-1"]["procurement"] == "build" and got["TOP-1"]["bom_level"] == 0
    assert got["TOP-1/ENC-1"]["procurement"] == "buy" and got["TOP-1/ENC-1/PEM-1"]["procurement"] == "reference"
    assert got["TOP-1/STK-1"]["procurement"] == "build"
    assert set(got["TOP-1/STK-1"]["procurement_basis"]["reasons"]) >= {"CONTAINS_CIRCUIT_ASSEMBLY"}
    assert got["TOP-1/STK-1/PCA-1"]["procurement"] == "build"
    assert "CONTAINS_BARE_BOARD" in got["TOP-1/STK-1/PCA-1"]["procurement_basis"]["reasons"]
    # Two boards per stack: 68 capacitors on boards, plus 3 more as a separate line (no merging).
    assert got["TOP-1/STK-1/PCA-1/CAP-1"]["quantity"] == "68" and got["TOP-1/CAP-1"]["quantity"] == "3"
    assert got["TOP-1/STK-1/PCA-1/DOC-1"]["procurement"] == "reference"
    assert got["TOP-1/STK-1/CBL-1"]["procurement"] == "buy" and got["TOP-1/STK-1/CBL-1/CON-1"]["procurement"] == "reference"
    assert result["summary"]["depth"] == 3 and result["summary"]["needs_decision"] == 0


def test_a_programmed_part_is_an_outside_service_with_the_blank_part_priced():
    lines, parents = tree([("IC-P", "IC, MCU, PROGRAMMED", 1, None), ("IC-B", "IC, MCU, BLANK", 1, 0)])
    result = resolve(lines, parents, proven=True)
    assert by_part(result)["IC-P"]["procurement"] == "service"
    assert by_part(result)["IC-P/IC-B"]["procurement"] == "buy"
    assert ("PROGRAMMING_CHARGE_TO_ADD", ("IC-P",)) in codes(result)


def test_conflicting_or_missing_clues_need_a_person():
    lines, parents = tree([("X-1", "CABLE ASSY WITH BOARD", 1, None), ("F-1", "PCB FABRICATION", 1, 0),
                           ("Y-1", "SUBASSEMBLY 7", 1, None), ("Z-1", "WIDGET", 2, 2)])
    result = resolve(lines, parents, proven=True)
    assert by_part(result)["X-1"]["procurement"] is None
    assert by_part(result)["Y-1"]["procurement_basis"] == {"kind": "needs_decision", "reasons": ["NO_CLUE"]}
    assert result["summary"]["needs_decision"] == 2
    assert all(f["severity"] == "blocking" for f in result["findings"])


def test_an_unproven_structure_is_flat_and_blocks_final_pricing():
    lines, parents = tree([("S-1", "PCB ASSY", 2, None), ("C-1", "CAP", 3, 0)])
    result = resolve(lines, parents, proven=False)
    assert [l["parent_line"] for l in result["lines"]] == [None, None]
    assert [l["quantity"] for l in result["lines"]] == ["2", "3"]
    assert codes(result) == [("STRUCTURE_NOT_PROVEN", ())]


def test_an_unreadable_quantity_and_bad_choices_are_loud():
    lines, parents = tree([("S-1", "PCB ASSY", "two", None), ("C-1", "CAP", 3, 0)])
    result = resolve(lines, parents, proven=True)
    assert ("QUANTITY_UNREADABLE", ("S-1",)) in codes(result)
    assert by_part(result)["S-1/C-1"]["quantity"] is None
    with pytest.raises(ValueError, match="CHOICE_INVALID"):
        resolve(lines, parents, proven=True, choices={"S-1": "make"})
    with pytest.raises(ValueError, match="CHOICE_UNKNOWN_LINE"):
        resolve(lines, parents, proven=True, choices={"C-1": "build"})


def test_a_board_document_is_not_a_bare_board_clue():
    lines, parents = tree([("K-1", "KIT 4", 1, None), ("D-1", "SCHEMATIC PCB KIT", 1, 0, "Document"),
                           ("A-1", "PCB ARTWORK KIT", 1, 0, "Document")])
    result = resolve(lines, parents, proven=True)
    assert by_part(result)["K-1"]["procurement_basis"] == {"kind": "needs_decision", "reasons": ["NO_CLUE"]}


def test_the_same_part_twice_under_one_parent_keeps_two_distinct_lines():
    lines, parents = tree([("S-1", "PCB ASSY", 1, None), ("C-1", "CAP", 3, 0), ("C-1", "CAP", 2, 0)])
    keys = [l["line_key"] for l in resolve(lines, parents, proven=True)["lines"]]
    assert keys == ["S-1", "S-1/C-1", "S-1/C-1#2"]
