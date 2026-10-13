"""One BOM holding several requested assemblies, through the whole package pipeline (QuickQuote A045).

Nick 2026-10-13: A045's only BOM lists both quoted boards in one sheet, each with its own lines under
a Parent Part column; each board's material must be only its own lines. A real parser (child process)
states parents in row map v2; validation proves them; review splits the file into one group per
requested board, binds each to its board, and resolution publishes one result per board from the one
document. The package choice result is supplied directly (its own tests cover how it is made).
Synthetic data only; no Azure/model calls. Run: pytest tests/test_combined_bom_package.py
"""
import pytest

from bom_processor import ParentPackageService, ProcessingRequest
from bom_processor import bom_package_choice
from bom_processor.package_resolution import PackageResolutionError
from test_subassembly_package_integration import RFQ, book, execute, published_rows

A1 = "aaaaaaaa-1111-4111-8111-111111111111"
A2 = "bbbbbbbb-2222-4222-8222-222222222222"
REQUESTED = [{"assembly_id": A1, "part_number": "0100-04931", "revision": "02"},
             {"assembly_id": A2, "part_number": "0100-04932", "revision": "02"}]
HEADER = ["Part", "Desc", "Qty", "Rev", "Maker", "MPN", "Parent"]
ROWS = [["0100-04931", "PCB ASSY, BCB", 1, "02", None, None, "0100-04931"],
        ["0120-05191", "PCB ARTWORK", 1, None, None, None, "0100-04931"],
        ["0110-04455", "PCB FABRICATION", 1, None, None, None, "0100-04931"],
        ["0630-03236", "CAP CER 33PF", 2, None, "KEMET", "C0805C330J5GACTU", "0100-04931"],
        ["0100-04932", "PCB ASSY, BCB", 1, "02", None, None, "0100-04932"],
        ["0120-05192", "PCB ARTWORK", 1, None, None, None, "0100-04932"],
        ["0110-04456", "PCB FABRICATION", 1, None, None, None, "0100-04932"],
        ["0630-03323", "2225 SIZE CAP", 3, None, "KEMET", "C2225C102KGRACTU", "0100-04932"]]


def pipeline(monkeypatch, rows=ROWS):
    store, validation = execute(through="validation", books=[("10055369-002", "1", book("10055369-002", "1", HEADER, rows))])
    assert validation.status == "awaiting_approval", validation
    source = next(item for item in [{"document_id": "doc-1", "blob_path": path, "sha256": None}
                                    for path in store.sources])
    import hashlib
    data = store.sources[source["blob_path"]]
    documents = [{"document_id": "doc-1", "blob_path": source["blob_path"], "size_bytes": len(data),
                  "sha256": hashlib.sha256(data).hexdigest()}]
    choice = {"version": bom_package_choice.RESULT_VERSION_V2, "request_id": "combined-choice", "model_calls": 0,
              "requested_assemblies": REQUESTED, "findings": [], "choices": [], "primary_selections": [],
              "survey": {"files": [], "findings": [], "has_blocking_findings": False},
              "documents": [{"document_id": "doc-1", "sha256": documents[0]["sha256"], "decision": "process",
                             "reason": "combined_bom", "disposition": None, "survey_file_ids": [], "roles": ["bom"],
                             "assemblies": [], "requested_assembly_ids": [A1, A2]}]}
    packages = ParentPackageService(store)
    plan = packages.plan(ProcessingRequest.from_dict({"job_id": "combined-demo", "customer_key": "customer",
        "parser_key": "Parser1", "rfq_uuid": RFQ, "package_version": "1", "documents": documents}),
        approved_assignments=[], pending_assignments=[{"request_id": validation.request_id, "document_ids": ["doc-1"]}])
    # The plan is bound to a completed package choice; its result is the one above.
    original = packages._plan
    state = {"request_id": "combined-choice", "status": "completed", "result_sha256": "c" * 64, "result": choice}
    monkeypatch.setattr(packages, "_plan", lambda plan_id: {**original(plan_id),
                                                            "package_choice": bom_package_choice.choice_reference(state)})
    monkeypatch.setattr(bom_package_choice, "bound_state", lambda storage, reference: state)
    return store, packages, plan["plan_id"]


def test_a_combined_bom_resolves_to_one_result_per_board_with_only_its_own_lines(monkeypatch):
    store, packages, plan_id = pipeline(monkeypatch)
    review = packages.review(plan_id)
    assert review["approval_ready"], review
    groups = [group for group in review["groups"] if group.get("status") == "ready"]
    assert sorted(group["evidence"]["assembly_identity"]["name"] for group in groups) == ["0100-04931", "0100-04932"]
    assert all(group["document_ids"] == ["doc-1"] for group in groups)
    # Each group is bound to its own requested board, from the line that heads its rows.
    assert sorted(group["evidence"]["requested_link"]["assembly_id"] for group in groups) == [A1, A2]
    approval = packages.approve(plan_id=plan_id, review_manifest_sha256=review["manifest_sha256"], approver="offline-test")
    resolved = packages.resolve(plan_id=plan_id, approval_id=approval["approval_id"])
    by_board = {result["assembly_identity"]["name"]: result for result in resolved["results"]}
    assert set(by_board) == {"0100-04931", "0100-04932"}
    parts = {board: sorted(str(row["item_number"]) for row in published_rows(store, result))
             for board, result in by_board.items()}
    # No line of one board is counted in the other board's material.
    assert parts["0100-04931"] == ["0100-04931", "0110-04455", "0120-05191", "0630-03236"]
    assert parts["0100-04932"] == ["0100-04932", "0110-04456", "0120-05192", "0630-03323"]
    subsets = [fixture["subset"] for result in resolved["results"] for fixture in result["provenance"]["fixtures"]]
    assert sorted(subset["assembly_id"] for subset in subsets) == [A1, A2]
    # Each board's structure is proven from the cells and holds only its own lines, under itself.
    import json
    for board, result in by_board.items():
        structure = json.loads(packages.resolved_structure(plan_id, approval["approval_id"], result["group_id"]))
        assert structure["proven"] is True, structure["findings"]
        keys = sorted(line["line_key"] for line in structure["lines"])
        assert all(key == board or key.startswith(board + "/") for key in keys), keys
        assert len(keys) == 4
    # Replays re-check the same coverage and return the same manifest.
    assert packages.resolve(plan_id=plan_id, approval_id=approval["approval_id"]) == resolved


def test_a_line_under_neither_board_stops_review_for_a_person(monkeypatch):
    stray = ROWS + [["9999-0001", "LABEL", 1, None, None, None, None]]
    _, packages, plan_id = pipeline(monkeypatch, stray)
    with pytest.raises(PackageResolutionError, match="SPLIT_ROW_UNASSIGNED"):
        packages.review(plan_id)
