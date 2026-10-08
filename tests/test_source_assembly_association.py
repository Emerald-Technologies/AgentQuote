"""Synthetic XLSX association integration; no model or external calls.

Inputs are unrelated filenames with source declarations and document references.
Checks validation/review/package resolution; run pytest on this file.
"""
import hashlib
import io
import json
from datetime import datetime, timezone

import pytest
from openpyxl import Workbook

from bom_processor import InMemoryStorage, ProcessingRequest, ParentPackageService
from bom_processor.assembly_association import VERSION, AssociationError, verify_association
from bom_processor.candidate_validation import CandidateValidationService, CandidateValidationError
from bom_processor.candidate_review import CandidateReviewService, CandidateReviewError
from bom_processor.core import CANONICAL_COLUMNS, OUTPUT_CONTENT_TYPE, _json_bytes
from bom_processor.foundry_author import build_complete_inventory
from bom_processor.job_orchestrator import StartResult


def workbook(rows):
    book = Workbook()
    book.active.title = "Customer"
    for row in rows:
        book.active.append(row)
    stream = io.BytesIO()
    book.save(stream)
    book.close()
    return stream.getvalue()


def proposal(source, name):
    return {"version": VERSION, "source_sha256": hashlib.sha256(source).hexdigest(),
            "name": {"value": name, "cell": {"sheet": "Customer", "row": 1, "column": 2}},
            "revision": {"value": "A", "cell": {"sheet": "Customer", "row": 2, "column": 2}},
            "rationale": "Agent proposal: explicit assembly and revision declarations above BOM.",
            "findings": []}


def roles(store, sources, role="bom"):
    """Synthetic agent role proposals citing the 'Part' header cell of each workbook."""
    from bom_processor.document_role import VERSION as ROLE_VERSION
    result = {}
    for source in sources:
        result[source["sha256"]] = {"version": ROLE_VERSION, "source_sha256": source["sha256"], "role": role,
            "rationale": "Synthetic: one row per part with quantity and manufacturer part number.",
            "evidence": [{"value": "Part", "cell": {"sheet": "Customer", "row": 3, "column": 1}}],
            "findings": []}
    return result


def setup(conflict=False):
    store = InMemoryStorage()
    sources, outputs, associations = [], {}, {}
    for index, (name, quantity, alternate) in enumerate([
            ("ASM-100", 2, False), ("ASM-100", 2, False),
            ("ASM-100", 3 if conflict else 2, True), ("ASM-200", 4, False)], 1):
        # doc-2 is an exact file alias, not a separately serialized equivalent
        # workbook whose ZIP timestamps can change its byte hash.
        source = (store.sources[sources[0]["blob_path"]] if index == 2 else
              workbook([["Assembly", name], ["Revision", "A"], ["Part", "Quantity", "Maker", "MPN"],
                           ["R1", quantity, "Acme", "M1"],
                           [None, None, "Other", "M2"] if alternate else ["Notes", "Ordinary layout"],
                   ["Drawing", "DRAW-1"], ["Procedure", "PROC-2"]]))
        digest = hashlib.sha256(source).hexdigest()
        path = ("customer/rfq-12345678-1234-4234-8234-123456789012/"
            f"package-1/original-boms/attachment-{index}.xlsx")
        store.sources[path] = source
        sources.append({"document_id": f"doc-{index}", "blob_path": path,
                        "sha256": digest, "size_bytes": len(source)})
        associations[digest] = proposal(source, name)
        rows = []
        for number, description, category, qty in [("DRAW-1", "Drawing", "Document", None),
                                                   ("PROC-2", "Procedure", "Document", None),
                                                   ("R1", "Resistor", "Part", quantity)]:
            row = dict.fromkeys(CANONICAL_COLUMNS)
            row.update(line_number=len(rows)+1, item_number=number, item_name=description,
                       item_category=category, quantity=qty)
            if category == "Part":
                row.update(manufacturer_1="Acme", manufacturer_item_number_1="M1")
                if alternate:
                    row.update(manufacturer_2="Other", manufacturer_item_number_2="M2")
            rows.append([row[key] for key in CANONICAL_COLUMNS])
        outputs[digest] = workbook([list(CANONICAL_COLUMNS), *rows])
    code = b"# Synthetic isolated-worker test double; never a live parser.\n"
    code_hash = hashlib.sha256(code).hexdigest()
    request_id = "xlsx-demo"
    candidate_id = hashlib.sha256(_json_bytes({"request_id": request_id, "code_sha256": code_hash,
        "signature": "a"*64, "authoring_version": "candidate-authoring.v5"})).hexdigest()
    root = f"candidates/{request_id}/{candidate_id}"
    store.candidates[root+"/parse.py"] = code
    store.candidates[root+"/candidate-manifest.json"] = _json_bytes({
        "version": "candidate-authoring.v5", "request_id": request_id, "candidate_id": candidate_id,
        "status": "generated_unvalidated", "code_sha256": code_hash})
    store.registry[f"candidates/requests/{request_id}.json"] = _json_bytes({
        "request_id": request_id, "status": "requested", "parser_key": "Parser1", "customer_key": "customer",
        "profile_hash": "a"*64, "profile": {"extension": ".xlsx"}, "source_evidence": sources})

    class Orchestrator:
        def start(self, item):
            output = outputs[item.input_sha256]
            store.candidates[item.output_blob] = output
            store.candidates[item.result_blob] = _json_bytes({"contract_version": "remote-worker.v1",
                "job": item.job_id, "status": "completed",
                "input": {"verified": True, "sha256": item.input_sha256, "size": item.input_size},
                "parser": {"verified": True, "sha256": item.parser_sha256, "size": item.parser_size},
                "output": {"sha256": hashlib.sha256(output).hexdigest(), "size": len(output),
                           "content_type": OUTPUT_CONTENT_TYPE}})
            return StartResult(item.job_id, "offline-test", datetime.now(timezone.utc))

    service = CandidateValidationService(request_id, candidate_id, store, Orchestrator(),
                                         assembly_associations=associations)
    return store, sources, associations, service


@pytest.mark.parametrize("conflict", [False, True])
def test_exact_alias_copies_bytes_even_when_workbook_metadata_changes(monkeypatch, conflict):
    import sys
    import zipfile
    module = sys.modules[__name__]
    original = workbook
    generated = []

    def changing_metadata(rows):
        raw = original(rows)
        archive = zipfile.ZipFile(io.BytesIO(raw))
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as output:
            for name in archive.namelist():
                # Deliberately distinct ZIP metadata; no real clock/wait required.
                entry = zipfile.ZipInfo(name, (2030, 1, 1, 0, 0, 2 * len(generated)))
                output.writestr(entry, archive.read(name))
        archive.close()
        value = stream.getvalue()
        generated.append(value)
        return value

    monkeypatch.setattr(module, "workbook", changing_metadata)
    store, sources, associations, _ = setup(conflict=conflict)
    first, alias = sources[:2]
    assert first["document_id"] != alias["document_id"]
    assert first["blob_path"] != alias["blob_path"]
    assert store.sources[first["blob_path"]] == store.sources[alias["blob_path"]]
    assert first["sha256"] == alias["sha256"]
    assert len({source["sha256"] for source in sources}) == len(associations) == 3
    assert len({value for value in generated}) == len(generated)


def test_two_assemblies_unrelated_filenames_documents_and_richer_bom_resolve():
    store, sources, _, service = setup()
    result = service.validate()
    review = CandidateReviewService(store).get_v3(result.request_id, result.candidate_id, result.validation_id)
    assert len(review["groups"]) == 2
    assert all(not fixture["mapping_required"] for group in review["groups"] for fixture in group["fixtures"])
    request = ProcessingRequest.from_dict({"job_id": "demo", "customer_key": "customer", "parser_key": "Parser1",
        "rfq_uuid": "12345678-1234-4234-8234-123456789012", "package_version": "1", "documents": sources})
    packages = ParentPackageService(store)
    plan = packages.plan(request, approved_assignments=[], pending_assignments=[{
        "request_id": result.request_id, "document_ids": [source["document_id"] for source in sources]}])
    package_review = packages.review(plan["plan_id"])
    assert package_review["approval_ready"]
    assert len(package_review["groups"]) == 2
    approved = packages.approve(plan_id=plan["plan_id"], review_manifest_sha256=package_review["manifest_sha256"],
                                 approver="offline-test")
    resolved = packages.resolve(plan_id=plan["plan_id"], approval_id=approved["approval_id"])
    assert len(resolved["results"]) == 2
    assert {item["assembly_identity"]["name"] for item in resolved["results"]} == {"asm-100", "asm-200"}


def test_material_conflict_is_an_exception_not_a_mapping_failure():
    store, _, _, service = setup(conflict=True)
    result = service.validate()
    review = CandidateReviewService(store).get_v3(result.request_id, result.candidate_id, result.validation_id)
    assert len(review["groups"]) == 2
    assert any(group["has_conflicts"] for group in review["groups"])
    assert review["approval_ready"] is False


def test_association_rechecked_at_review_and_source_tamper_rejected():
    store, sources, _, service = setup()
    result = service.validate()
    store.sources[sources[0]["blob_path"]] = b"tampered"
    with pytest.raises(CandidateReviewError, match="REVIEW_ASSOCIATION_BINDING"):
        CandidateReviewService(store).get_v3(result.request_id, result.candidate_id, result.validation_id)


def test_false_value_blocking_findings_and_incomplete_coverage_are_held():
    store, sources, associations, service = setup()
    first = sources[0]
    associations[first["sha256"]]["name"]["value"] = "OTHER"
    with pytest.raises(AssociationError, match="ASSOCIATION_SOURCE_VALUE_CHANGED"):
        verify_association(store.sources[first["blob_path"]], ".xlsx", associations[first["sha256"]])
    associations.pop(first["sha256"])
    with pytest.raises(CandidateValidationError, match="ASSEMBLY_ASSOCIATION_COVERAGE_INVALID"):
        service.validate()


def test_complete_inventory_includes_late_rows_and_refuses_truncation():
    source = workbook([["row", index] for index in range(50)])
    context = json.loads(build_complete_inventory(source, "xlsx"))
    rows = context["sheets"][0]["rows"]
    assert context["coverage"] == "complete" and sum(len(cells) for _, cells in rows) == 100
    assert rows[-1] == [50, [[1, "row"], [2, 49]]]
    with pytest.raises(ValueError, match="AUTHORING_SOURCE_CONTEXT_LIMIT"):
        build_complete_inventory(workbook([["short words "*20] for _ in range(1000)]), "xlsx")


def test_v4_legacy_review_rejected_and_replay_is_stable():
    store, _, _, service = setup()
    result = service.validate()
    assert service.validate() == result
    with pytest.raises(CandidateReviewError, match="REVIEW_ASSOCIATION_AWARE_CONTRACT_REQUIRED"):
        CandidateReviewService(store).get(result.request_id, result.candidate_id, result.validation_id)


def test_blocking_association_is_held_before_worker_execution():
    _, sources, associations, service = setup()
    associations[sources[0]["sha256"]]["findings"] = [{
        "code": "AMBIGUOUS_ASSEMBLY", "severity": "blocking", "rationale": "Competing declarations."}]
    with pytest.raises(CandidateValidationError, match="ASSEMBLY_ASSOCIATION_REVIEW_REQUIRED"):
        service.validate()


def test_sensitive_sheet_metadata_rejected_before_agent_construction():
    from types import SimpleNamespace
    from bom_processor.foundry_author import FoundryModelAuthor
    book = Workbook()
    book.active.title = "a@example.com"
    book.active.append(["Part", "R1"])
    stream = io.BytesIO()
    book.save(stream)
    book.close()
    calls = []
    prompt = SimpleNamespace(structural_profile={"source_visibility": "complete-inventory.v1"},
                             source_analysis=SimpleNamespace(extension="xlsx"))
    with pytest.raises(ValueError, match="AUTHORING_SOURCE_REDACTION_REQUIRES_REVIEW"):
        FoundryModelAuthor(agent_factory=lambda **kwargs: calls.append(kwargs)).generate(prompt, stream.getvalue())
    assert calls == []


def test_v4_promotion_requires_review_hash_and_conflict_disposition():
    from bom_processor.admin_promotion import AdminDecisionService
    from bom_processor.master_registry import RegistryError
    store, _, _, service = setup(conflict=True)
    result = service.validate()
    review = CandidateReviewService(store).get_v3(result.request_id, result.candidate_id, result.validation_id)
    common = {"request_id": result.request_id, "candidate_id": result.candidate_id,
              "validation_id": result.validation_id, "approver": {"object_id": "offline-admin"},
              "decision": "approve", "variant_id": "xlsx-demo", "version": "1", "reason": "", "ticket": ""}
    admin = AdminDecisionService(store)
    with pytest.raises(RegistryError, match="REVIEW_MANIFEST_BINDING"):
        admin.decide(**common)
    with pytest.raises(RegistryError, match="FIXTURE_SELECTION_INVALID"):
        admin.decide(**common, review_manifest_sha256=review["evidence"]["manifest_sha256"])
    assert not any(path.startswith("approved/") for path in store.registry)