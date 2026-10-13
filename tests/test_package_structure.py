"""Structure published with a resolved package (sub-assembly plan unit 3b, Nick 2026-10-12).

A one-document candidate package whose parser stated parents from a Level column. Resolution
re-checks the row map against the source, rolls quantities down, proposes build or buy and publishes
a structure file beside the (re-sorted) workbook under manifest v2. Flat packages stay v1.
Synthetic data only."""
import hashlib
import io
import json

import pandas as pd
import pytest

from bom_processor import InMemoryStorage, PackageResolutionError, ParentPackageService, ProcessingRequest
from bom_processor.core import CANONICAL_COLUMNS, OUTPUT_CONTENT_TYPE
from bom_processor.row_accounting import VERSION as ROW_MAP_VERSION
from bom_processor.semantic_facts import facts_from_xlsx

RFQ = "12345678-1234-4234-8234-123456789012"
CODE = b"# parser\n"
# Level, Type, Part, Rev, Desc, Qty
SOURCE = (b"Level,Type,Part,Rev,Desc,Qty\n0,Document,Board,A,Board,1\n1,Part,PCA-1,,PCB ASSY MAIN,2\n"
          b"2,Part,CAP-1,,CAP CER,3\n1,Part,CBL-1,,CABLE ASSY FAN,1\n2,Part,CON-1,,CONNECTOR,2\n")
PARENTS = [None, 2, 3, 2, 5]  # output rows: Board 2, PCA-1 3, CAP-1 4, CBL-1 5, CON-1 6


def output_workbook():
    rows = []
    for number, (part, rev, desc, qty, category) in enumerate([
            ("Board", "A", "Board", 1, "Document"), ("PCA-1", None, "PCB ASSY MAIN", 2, "Part"),
            ("CAP-1", None, "CAP CER", 3, "Part"), ("CBL-1", None, "CABLE ASSY FAN", 1, "Part"),
            ("CON-1", None, "CONNECTOR", 2, "Part")], 1):
        row = dict.fromkeys(CANONICAL_COLUMNS)
        row.update(line_number=number, item_number=part, revision=rev, item_name=desc,
                   item_category=category, quantity=qty)
        rows.append(row)
    stream = io.BytesIO()
    pd.DataFrame(rows, columns=CANONICAL_COLUMNS).to_excel(stream, index=False)
    return stream.getvalue()


def row_map(parents=PARENTS):
    lines = []
    for index, parent in enumerate(parents):
        source_row = index + 2
        fields = {"item_number": [source_row, 3], "item_name": [source_row, 5], "quantity": [source_row, 6]}
        if index == 0:
            fields["revision"] = [source_row, 4]
        lines.append({"output_row": index + 2, "sheet": "CSV", "rows": [source_row], "fields": fields,
                      "parent": {"line": parent, "from": [source_row, 1], "by": "level"}})
    return {"version": ROW_MAP_VERSION, "lines": lines,
            "skipped": [{"sheet": "CSV", "rows": [[1, 1]], "reason": "header", "rationale": "Headings."}],
            "ignored_columns": [{"sheet": "CSV", "column": 2, "reason": "Type decides the category."}]}


def flat_row_map():
    mapped = row_map()
    for line in mapped["lines"]:
        del line["parent"]
    mapped["ignored_columns"].append({"sheet": "CSV", "column": 1, "reason": "Level."})
    return mapped


def build(mapped):
    source_path = f"cust/rfq-{RFQ}/package-1/original-boms/doc-1.csv"
    digest = hashlib.sha256(SOURCE).hexdigest()
    request = ProcessingRequest.from_dict({"job_id": "job-1", "customer_key": "cust", "parser_key": "Parser1",
        "rfq_uuid": RFQ, "package_version": "1", "documents": [
            {"document_id": "doc-1", "blob_path": source_path, "size_bytes": len(SOURCE), "sha256": digest}]})
    request_id, candidate_id, validation_id = "request-1", "candidate-1", "validation-1"
    prefix = f"candidates/{request_id}/{candidate_id}/validation/{validation_id}/"
    output = output_workbook()
    mapped_bytes = json.dumps(mapped).encode()
    facts = facts_from_xlsx(output)
    candidates = {
        f"candidates/{request_id}/{candidate_id}/parse.py": CODE,
        f"candidates/{request_id}/{candidate_id}/candidate-manifest.json": json.dumps({
            "version": "candidate-authoring.v9", "request_id": request_id, "candidate_id": candidate_id,
            "status": "generated_unvalidated", "code_sha256": hashlib.sha256(CODE).hexdigest()}).encode(),
        f"{prefix}worker/output-1.xlsx": output,
        f"{prefix}worker/row-map.json": mapped_bytes,
    }
    fixture = {"fixture_sha256": digest, "status": "passed",
               "source_aliases": [{"document_id": "doc-1", "blob_path": source_path}],
               "output": {"blob_path": f"{prefix}worker/output-1.xlsx", "sha256": hashlib.sha256(output).hexdigest(),
                          "size_bytes": len(output), "content_type": OUTPUT_CONTENT_TYPE},
               "semantic": {"valid": True, "fingerprint": facts.fingerprint,
                            "assembly_identity": {"name": "board", "revision": "a"},
                            "model_summary": facts.model_summary,
                            "row_accounting": {"version": "bom-row-accounting.v1", "valid": True,
                                               "row_map": {"blob_path": f"{prefix}worker/row-map.json",
                                                           "sha256": hashlib.sha256(mapped_bytes).hexdigest(),
                                                           "size": len(mapped_bytes)}}}}
    candidates[f"{prefix}evidence.json"] = json.dumps({
        "version": "candidate-validation.v3", "request_id": request_id, "candidate_id": candidate_id,
        "validation_id": validation_id, "status": "awaiting_approval", "complete": True, "fixture_count": 1,
        "candidate": {"blob_path": f"candidates/{request_id}/{candidate_id}/parse.py",
                      "sha256": hashlib.sha256(CODE).hexdigest(), "size_bytes": len(CODE)},
        "fixtures": [fixture, {"fixture_sha256": "f" * 64, "status": "passed", "source_aliases": []}][:1],
        "policy_versions": []}).encode()
    store = InMemoryStorage(sources={source_path: SOURCE}, candidates=candidates)
    store.registry[f"candidates/requests/{request_id}.json"] = json.dumps({
        "version": "candidate-request.v2", "request_id": request_id, "status": "requested",
        "parser_key": "Parser1", "customer_key": "cust", "profile_hash": "a" * 64, "profile": {"extension": ".csv"},
        "source_evidence": [{"document_id": "doc-1", "blob_path": source_path,
                             "size_bytes": len(SOURCE), "sha256": digest}]}).encode()
    service = ParentPackageService(store)
    package = service.plan(request, approved_assignments=[], pending_assignments=[
        {"request_id": request_id, "document_ids": ["doc-1"]}])
    review = service.review(package["plan_id"])
    assert review["approval_ready"], review
    approval = service.approve(plan_id=package["plan_id"], review_manifest_sha256=review["manifest_sha256"],
                               approver="admin-1")
    return store, service, package["plan_id"], approval["approval_id"]


def resolved(mapped):
    store, service, plan_id, approval_id = build(mapped)
    manifest = service.resolve(plan_id=plan_id, approval_id=approval_id)
    return store, service, plan_id, approval_id, manifest


def test_a_structured_bom_publishes_a_structure_file_that_follows_the_resorted_rows():
    _, service, plan_id, approval_id, manifest = resolved(row_map())
    assert manifest["version"] == "resolved-output-manifest.v2"
    group_id = manifest["results"][0]["group_id"]
    structure = service.resolved_structure(plan_id, approval_id, group_id)
    workbook = pd.read_excel(io.BytesIO(service.resolved_workbook(plan_id, approval_id, group_id)))
    published = {int(row.line_number): row.item_number for row in workbook.itertuples()}
    by_key = {line["line_key"]: line for line in structure["lines"]}
    # Every structure line names the published row it describes, after reconciliation's re-sort.
    for line in structure["lines"]:
        assert published[line["line_number"]] == line["line_key"].split("/")[-1]
    assert structure["proven"] is True
    cap = by_key["Board/PCA-1/CAP-1"]
    assert (cap["quantity"], cap["quantity_per_parent"], cap["procurement"]) == ("6", "3", "buy")
    assert by_key["Board/PCA-1"]["procurement"] == "build"
    assert by_key["Board/CBL-1"]["procurement"] == "buy"
    assert by_key["Board/CBL-1/CON-1"]["procurement"] == "reference"
    assert published[cap["parent_line_number"]] == "PCA-1"
    # Read-only replays return the same manifest.
    assert service.manifest(plan_id, approval_id) == manifest


def test_a_flat_bom_is_published_as_before():
    _, service, plan_id, approval_id, manifest = resolved(flat_row_map())
    assert manifest["version"] == "resolved-output-manifest.v1"
    assert "structure" not in manifest["results"][0]
    with pytest.raises(PackageResolutionError, match="RESOLVED_STRUCTURE_ABSENT"):
        service.resolved_structure(plan_id, approval_id, manifest["results"][0]["group_id"])


def test_wrong_parent_claims_publish_an_unproven_structure_that_blocks_final_pricing():
    wrong = list(PARENTS); wrong[4] = 3  # CON-1 claimed under PCA-1
    _, service, plan_id, approval_id, manifest = resolved(row_map(wrong))
    structure = service.resolved_structure(plan_id, approval_id, manifest["results"][0]["group_id"])
    assert structure["proven"] is False
    assert [f["code"] for f in structure["findings"]] == ["STRUCTURE_NOT_PROVEN"]
    assert all(line["parent_line_number"] is None for line in structure["lines"])


def test_a_tampered_structure_file_is_refused():
    store, service, plan_id, approval_id, manifest = resolved(row_map())
    path = manifest["results"][0]["structure"]["blob_path"]
    store.outputs[path] = store.outputs[path].replace(b'"build"', b'"buy__"')
    with pytest.raises(PackageResolutionError, match="RESOLVED_OUTPUT_CONFLICT"):
        service.manifest(plan_id, approval_id)
