"""Separate sub-assembly BOM files through the whole package pipeline (sub-assembly plan unit 6).

QuickQuote Docs/SUBASSEMBLY_BOM_PLAN_2026-10-12.md section 13 (Nick 2026-10-13: attach under the naming
line, or compare when the parent already lists the parts; a file named twice is used for each; an
unclear match is a warning and is not attached). Same offline pipeline as
test_subassembly_package_integration (fake model, real parser child process). Synthetic data only.
Run: pytest tests/test_package_attachment.py
"""
import json

import pytest

from bom_processor import bom_attachment
from test_subassembly_package_integration import book, execute, published_rows

LEVEL = ["Part", "Desc", "Qty", "Rev", "Level", "Maker", "MPN"]
FLAT = ["Part", "Desc", "Qty", "Maker", "MPN"]
SCREW = ["SCR-1", "SCREW", 4, None, 1, "MCMASTER", "91292A113"]
CABLE = ("CBL-9", "A", book("CBL-9", "A", FLAT, [["WIRE-1", "WIRE 22AWG", 3, "ALPHA WIRE", "5854"], ["CON-9", "CONNECTOR", 2, "MOLEX", "43025-0400"]]))


def top(*lines, name="ASM-T"):
    return (name, "A", book(name, "A", LEVEL, [[name, "TOP ASSEMBLY", 1, "A", 0, None, None], *lines]))


def structure_of(packages, plan_id, approval_id, result):
    return json.loads(packages.resolved_structure(plan_id, approval_id, result["group_id"]))


def by_key(structure):
    return {line["line_key"]: line for line in structure["lines"]}


def codes(review):
    return sorted({item["code"] for item in review.get("subassembly_findings", [])})


def run(books):
    store, packages, plan_id, approval_id, resolved = execute(books=books)
    approval = packages.approval(plan_id)
    return store, packages, plan_id, approval_id, resolved, approval["review"]


def test_a_cable_file_goes_under_the_line_that_names_it():
    store, packages, plan_id, approval_id, resolved, review = run([
        top(["CBL-9", "CABLE ASSY FAN", 2, "A", 1], SCREW), CABLE])
    assert [group["status"] for group in review["groups"]] == ["ready"]
    assert codes(review) == ["SUBASSEMBLY_FILE_ATTACHED"]
    assert resolved["version"] == "resolved-output-manifest.v2"
    (result,) = resolved["results"]
    assert (result["document_ids"], result["subassembly_document_ids"]) == (["doc-1"], ["doc-2"])
    structure = structure_of(packages, plan_id, approval_id, result)
    lines = by_key(structure)
    assert structure["proven"] is True
    cable, wire = lines["ASM-T/CBL-9"], lines["ASM-T/CBL-9/WIRE-1"]
    assert (cable["is_subassembly"], cable["procurement"]) == (True, "buy")
    assert (wire["quantity_per_parent"], wire["quantity"], wire["procurement"]) == ("3", "6", "reference")
    assert lines["ASM-T/SCR-1"]["quantity"] == "4"
    assert structure["subassembly_files"] == [{"action": "attached", "line_number": cable["line_number"],
        "assembly_identity": {"name": "cbl-9", "revision": "a"}, "document_ids": ["doc-2"]}]
    rows = published_rows(store, result)
    # The file's rows follow the naming line, and every structure line names its published row.
    position = [row["item_number"].upper() for row in rows].index("CBL-9")
    assert {rows[position + 1]["item_number"].upper(), rows[position + 2]["item_number"].upper()} == {"WIRE-1", "CON-9"}
    for line in structure["lines"]:
        assert rows[line["line_number"] - 1]["item_number"].upper() == line["line_key"].split("/")[-1]
    assert packages.manifest(plan_id, approval_id) == resolved


def test_a_file_named_twice_is_used_for_each_and_files_nest():
    kit = ("KIT-5", "A", book("KIT-5", "A", FLAT, [["CBL-9", "CABLE ASSY FAN", 1, None, None], ["LBL-1", "LABEL", 1, "BRADY", "THT-1"]]))
    _, packages, plan_id, approval_id, resolved, review = run([
        top(["CBL-9", "CABLE ASSY FAN", 2, "A", 1], ["KIT-5", "SUBASSEMBLY 7", 1, "A", 1], SCREW), CABLE, kit])
    assert codes(review) == ["SUBASSEMBLY_FILE_ATTACHED", "SUBASSEMBLY_FILE_SHARED"]
    (result,) = resolved["results"]
    assert result["subassembly_document_ids"] == ["doc-2", "doc-3"]
    lines = by_key(structure_of(packages, plan_id, approval_id, result))
    assert lines["ASM-T/CBL-9/WIRE-1"]["quantity"] == "6"
    assert lines["ASM-T/KIT-5/CBL-9/WIRE-1"]["quantity"] == "3"
    assert lines["ASM-T/KIT-5"]["procurement_basis"]["kind"] == "needs_decision"


@pytest.mark.parametrize("quantity, outcome", [(3, "SUBASSEMBLY_FILE_MATCHES"), (4, "SUBASSEMBLY_FILE_DIFFERS")])
def test_a_file_whose_parts_are_already_listed_is_only_compared(quantity, outcome):
    board = ("PCA-1", "A", book("PCA-1", "A", FLAT, [["CAP-1", "CAP CER", quantity, "MURATA", "GRM188"]]))
    _, packages, plan_id, approval_id, resolved, review = run([
        top(["PCA-1", "PCB ASSY MAIN", 2, "A", 1], ["CAP-1", "CAP CER", 3, None, 2, "MURATA", "GRM188"]), board])
    assert codes(review) == [outcome]
    finding = review["subassembly_findings"][0]
    assert finding["severity"] == ("information" if quantity == 3 else "warning")
    (result,) = resolved["results"]
    assert result["subassembly_document_ids"] == ["doc-2"]
    lines = by_key(structure_of(packages, plan_id, approval_id, result))
    # Nothing is added twice: the board's parts stay the parent's own lines.
    assert sorted(lines) == ["ASM-T", "ASM-T/PCA-1", "ASM-T/PCA-1/CAP-1"]
    assert lines["ASM-T/PCA-1/CAP-1"]["quantity"] == "6"


def test_an_unclear_match_is_set_aside_with_a_warning():
    _, packages, plan_id, approval_id, resolved, review = run([
        top(["CBL-9", "CABLE ASSY FAN", 2, "B", 1], SCREW), CABLE])
    assert codes(review) == ["SUBASSEMBLY_FILE_REVISION_DIFFERS"]
    aside = [group for group in review["groups"] if group["status"] == "excluded"]
    assert [(group["document_ids"], group["reason"]) for group in aside] == [(["doc-2"], "subassembly_unclear")]
    (result,) = resolved["results"]
    assert "subassembly_document_ids" not in result
    lines = by_key(structure_of(packages, plan_id, approval_id, result))
    assert lines["ASM-T/CBL-9"]["is_subassembly"] is False


def test_matching_rules():
    assert bom_attachment.names("ENE010-0810-U2-TK", "010-0810-U2")
    assert not bom_attachment.names("010-0810-U2-03", "010-0810-U2")
    assert not bom_attachment.names("ASM-1-SUB", "ASM-1")
    nodes = [bom_attachment.Node(0, "TOP-1", "", (("TOP-1", ""), ("CBL-9", ""))),
             bom_attachment.Node(1, "CBL-9", "", (("W-1", ""),)),
             bom_attachment.Node(2, "CBL-9", "", (("W-2", ""),))]
    decided = bom_attachment.plan(nodes, lambda key, position: False)
    # Two files for one line: neither is attached; both are set aside with a warning.
    assert decided["attach"] == {} and decided["set_aside"] == [1, 2]
    assert {item["code"] for item in decided["findings"]} == {"SUBASSEMBLY_FILE_AMBIGUOUS"}
    cycle = [bom_attachment.Node(0, "A-100", "", (("B-200", ""),)), bom_attachment.Node(1, "B-200", "", (("A-100", ""),))]
    assert bom_attachment.plan(cycle, lambda key, position: False)["attach"] == {}
    assert bom_attachment.compare([("C1", 3), ("C2", "1")], [("c1", "3.0"), ("C2", 1)])["outcome"] == "matches"
    assert bom_attachment.compare([("C1", 3)], [("C1", 3), ("C3", 1)]) == {
        "outcome": "differs", "in_both": 1, "only_in_file": 1, "only_in_parent": 0, "quantity_differs": 0}


@pytest.mark.parametrize("revision, accepted", [("A", True), ("B", False)])
def test_system_acceptance_takes_attached_files_and_refuses_unclear_ones(revision, accepted):
    from bom_processor import PackageResolutionError
    from bom_processor.package_system_acceptance import SystemPackageService
    store, packages, plan_id, _, _, review = run([top(["CBL-9", "CABLE ASSY FAN", 2, revision, 1], SCREW), CABLE])
    system = SystemPackageService(store)
    accept = lambda: system.accept_system(plan_id=plan_id, review_manifest_sha256=review["manifest_sha256"],
        assembly_identities=[{"name": "ASM-T", "revision": "A"}], caller_application="offline-test", _persist=False)
    if accepted:
        assert accept()["review"]["groups"][0]["subassembly_document_ids"] == ["doc-2"]
    else:
        with pytest.raises(PackageResolutionError, match="SYSTEM_REVIEW_NOT_QUALIFIED"):
            accept()
