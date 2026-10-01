import base64
import hashlib
import io
import json
from email.message import Message

import pandas as pd
import pytest

from bom_processor import (InMemoryStorage, PackageResolutionError,
                           ParentPackageService, ProcessingRequest,
                           CandidateStatusService, CandidateValidationService)
from bom_processor.core import CANONICAL_COLUMNS, OUTPUT_CONTENT_TYPE
from bom_processor.core import _json_bytes
from bom_processor.job_orchestrator import StartResult
from bom_processor.semantic import AssemblyIdentity, normalize_bom
from bom_processor.semantic_facts import facts_from_xlsx
from deterministic_api import ApiConfig, PackageApi

RFQ = "12345678-1234-4234-8234-123456789012"
CODE = b"# parser\n"
CODE_SHA = hashlib.sha256(CODE).hexdigest()


def package_workbook(*, mpn="100", uom="EA", quantity=2, assembly=None,
                     document_rows=None, mpns=None):
    rows = []
    identities = document_rows if document_rows is not None else ([] if assembly is None else [assembly])
    for document_identity in identities:
        row = {name: None for name in CANONICAL_COLUMNS}
        row.update(line_number=len(rows) + 1, item_number=document_identity[0], item_name=document_identity[0],
                   revision=document_identity[1], item_category="Document", quantity=1)
        rows.append(row)
    row = {name: None for name in CANONICAL_COLUMNS}
    row.update(line_number=len(rows) + 1, item_number="R1", item_name="Resistor",
               item_category="Part", unit_of_measure=uom, quantity=quantity,
               reference_designator="R1", manufacturer_1="Acme",
               manufacturer_item_number_1=mpn)
    if mpns is not None:
        for index in range(1, 9):
            row[f"manufacturer_{index}"] = "Acme" if index <= len(mpns) else None
            row[f"manufacturer_item_number_{index}"] = mpns[index - 1] if index <= len(mpns) else None
    rows.append(row)
    stream = io.BytesIO()
    pd.DataFrame(rows, columns=CANONICAL_COLUMNS).to_excel(stream, index=False)
    return stream.getvalue()


@pytest.mark.parametrize("count", [0, 1, 3, 8])
def test_shared_semantic_facts_count_mpns_without_nan(count):
    from bom_processor.semantic_facts import facts_from_xlsx
    facts = facts_from_xlsx(package_workbook(assembly=("Board", "A"),
                                             mpns=[str(index) for index in range(count)]))
    assert facts.model_summary["mpn_count"] == count


@pytest.mark.parametrize("count", [0, 1, 3, 8])
def test_validation_status_and_parent_review_share_exact_semantic_facts(count):
    source = b"Part,Qty\nR1,1\n"
    source_path = f"cust/rfq-{RFQ}/package-1/original-boms/doc-1.csv"
    profile_hash = "a" * 64
    request_id = f"request-{count}"
    code = b"# candidate parser\n"
    candidate_id = hashlib.sha256(_json_bytes({
        "request_id": request_id, "code_sha256": hashlib.sha256(code).hexdigest(),
        "signature": profile_hash, "authoring_version": "candidate-authoring.v3",
    })).hexdigest()
    output = package_workbook(assembly=("Board", "A"),
                              mpns=[str(index + 1) for index in range(count)])
    store = InMemoryStorage(sources={source_path: source}, candidates={
        f"candidates/{request_id}/{candidate_id}/parse.py": code,
        f"candidates/{request_id}/{candidate_id}/candidate-manifest.json": _json_bytes({
            "version": "candidate-authoring.v3", "request_id": request_id,
            "candidate_id": candidate_id, "status": "generated_unvalidated",
            "code_sha256": hashlib.sha256(code).hexdigest()}),
    })
    source_evidence = {"document_id": "doc-1", "blob_path": source_path,
                       "size_bytes": len(source), "sha256": hashlib.sha256(source).hexdigest()}
    store.registry[f"candidates/requests/{request_id}.json"] = _json_bytes({
        "version": "candidate-request.v2", "request_id": request_id, "status": "requested",
        "parser_key": "Parser1", "customer_key": "cust", "profile_hash": profile_hash,
        "profile": {"extension": ".csv"}, "source_evidence": [source_evidence]})

    class Orchestrator:
        def start(self, item):
            store.candidates[item.output_blob] = output
            store.candidates[item.result_blob] = _json_bytes({
                "contract_version": "remote-worker.v1", "job": item.job_id, "status": "completed",
                "input": {"verified": True, "sha256": item.input_sha256, "size": item.input_size},
                "parser": {"verified": True, "sha256": item.parser_sha256, "size": item.parser_size},
                "output": {"content_type": OUTPUT_CONTENT_TYPE, "sha256": hashlib.sha256(output).hexdigest(),
                           "size": len(output)}})
            return StartResult(item.job_id, "execution-1", pd.Timestamp("2026-01-01", tz="UTC").to_pydatetime())

    validated = CandidateValidationService(request_id, candidate_id, store, Orchestrator()).validate()
    status = CandidateStatusService(store).get(request_id)
    assert status["status"] == "awaiting_approval"
    assert status["review"]["groups"][0]["fixtures"][0]["model_summary"]["mpn_count"] == count
    request = ProcessingRequest.from_dict({"job_id": f"job-{count}", "customer_key": "cust",
        "parser_key": "Parser1", "rfq_uuid": RFQ, "package_version": "1",
        "documents": [source_evidence]})
    package = ParentPackageService(store).plan(request, approved_assignments=[], pending_assignments=[{
        "request_id": request_id, "document_ids": ["doc-1"]}])
    review = ParentPackageService(store).review(package["plan_id"])
    assert validated.status == "awaiting_approval" and review["approval_ready"]
    assert review["groups"][0]["evidence"]["fixtures"][0]["model_summary"]["mpn_count"] == count


def test_document_identity_is_order_independent_and_conflicts_are_explicit():
    from bom_processor.semantic_facts import SemanticFactsError, facts_from_xlsx
    forward = facts_from_xlsx(package_workbook(document_rows=[("Board", "A"), ("Board", "A")]))
    reverse = facts_from_xlsx(package_workbook(document_rows=[("Board", "A"), ("Board", "A")][::-1]))
    assert forward.assembly == reverse.assembly
    with pytest.raises(SemanticFactsError, match="ASSEMBLY_IDENTITY_CONFLICT"):
        facts_from_xlsx(package_workbook(document_rows=[("Board", "A"), ("Other", "B")]))


def request_and_store(outputs=None, identities=None, claimed_identities=None):
    """Build a two-source candidate package fixture.

    identities: byte-derived assembly identities (Document rows) per output.
    claimed_identities: optional override of what the evidence claims, to
    exercise forged-identity rejection.
    """
    identities = identities if identities is not None else [("Board", "A"), ("Board", "A")]
    outputs = outputs if outputs is not None else [
        package_workbook(assembly=identities[0]),
        package_workbook(mpn="200", assembly=identities[1]),
    ]
    claimed_identities = claimed_identities if claimed_identities is not None else identities
    sources = {f"cust/rfq-{RFQ}/package-1/original-boms/doc-{i}.csv": f"source-{i}".encode()
               for i in range(1, 3)}
    request = ProcessingRequest.from_dict({"job_id": "job-1", "customer_key": "cust",
        "parser_key": "Parser1", "rfq_uuid": RFQ, "package_version": "1",
        "documents": [{"document_id": f"doc-{i}", "blob_path": path,
                       "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                      for i, (path, data) in enumerate(sources.items(), 1)]})
    request_id, candidate_id, validation_id = "request-1", "candidate-1", "validation-1"
    fixtures = []
    candidates = {f"candidates/{request_id}/{candidate_id}/parse.py": CODE,
        f"candidates/{request_id}/{candidate_id}/candidate-manifest.json": json.dumps({
            "version": "candidate-authoring.v3", "request_id": request_id,
            "candidate_id": candidate_id, "status": "generated_unvalidated",
            "code_sha256": CODE_SHA}).encode()}
    source_evidence = []
    for index, (document, output, identity, claimed) in enumerate(
            zip(request.documents, outputs, identities, claimed_identities), 1):
        digest = document.sha256
        output_path = (f"candidates/{request_id}/{candidate_id}/validation/{validation_id}/"
                       f"worker/output-{index}.xlsx")
        candidates[output_path] = output
        facts = facts_from_xlsx(output)
        source_evidence.append({"document_id": document.document_id, "blob_path": document.blob_path,
                                "size_bytes": document.size_bytes, "sha256": digest})
        fixtures.append({"fixture_sha256": digest, "status": "passed",
            "source_aliases": [{"document_id": document.document_id, "blob_path": document.blob_path}],
            "output": {"blob_path": output_path, "sha256": hashlib.sha256(output).hexdigest(),
                       "size_bytes": len(output), "content_type": OUTPUT_CONTENT_TYPE},
            "semantic": {"valid": True, "fingerprint": facts.fingerprint,
                "assembly_identity": ({"name": claimed[0].casefold(), "revision": claimed[1].casefold()}
                                      if claimed is not None else None),
                "model_summary": facts.model_summary}})
    candidates[f"candidates/{request_id}/{candidate_id}/validation/{validation_id}/evidence.json"] = json.dumps({
        "version": "candidate-validation.v3", "request_id": request_id,
        "candidate_id": candidate_id, "validation_id": validation_id,
        "status": "awaiting_approval", "complete": True, "fixture_count": 2,
        "candidate": {"blob_path": f"candidates/{request_id}/{candidate_id}/parse.py",
                      "sha256": CODE_SHA, "size_bytes": len(CODE)},
        "fixtures": fixtures, "policy_versions": []}).encode()
    store = InMemoryStorage(sources=sources, candidates=candidates)
    store.registry[f"candidates/requests/{request_id}.json"] = json.dumps({
        "version": "candidate-request.v2", "request_id": request_id, "status": "requested",
        "parser_key": "Parser1", "customer_key": "cust", "profile_hash": "a" * 64,
        "profile": {"extension": ".csv"}, "source_evidence": source_evidence}).encode()
    return request, store


def plan(service, request):
    return service.plan(request, approved_assignments=[], pending_assignments=[{
        "request_id": "request-1", "document_ids": ["doc-1", "doc-2"]}])


def approve(service, package, **kwargs):
    review = service.review(package["plan_id"], **kwargs)
    assert review["approval_ready"], review
    return review, service.approve(plan_id=package["plan_id"],
                                   review_manifest_sha256=review["manifest_sha256"],
                                   approver="admin-1", **kwargs)


def test_pending_plan_verifies_sources_and_projects_child_state_dynamically():
    request, store = request_and_store()
    service = ParentPackageService(store)
    package = plan(service, request)
    assert package["version"] == "package-plan.v1"
    assert package["assignments"][0]["request_id"] == "request-1"
    assert "candidate_id" not in package["assignments"][0]
    review = service.review(package["plan_id"])
    assert review["version"] == "package-review.v1"
    assert review["approval_ready"] and review["groups"][0]["candidate_id"] == "candidate-1"
    path = "candidates/request-1/candidate-1/validation/validation-1/evidence.json"
    evidence = json.loads(store.candidates[path])
    evidence["status"] = "generated_unvalidated"
    evidence["fixtures"][0]["status"] = "failed"
    store.candidates[path] = json.dumps(evidence).encode()
    assert service.review(package["plan_id"])["status"] == "pending"


def test_source_tamper_and_malicious_assignment_paths_fail_before_persistence():
    request, store = request_and_store()
    service = ParentPackageService(store)
    store.sources[request.documents[0].blob_path] = b"tampered"
    with pytest.raises(PackageResolutionError, match="PACKAGE_SOURCE_INVALID"):
        plan(service, request)
    request, store = request_and_store()
    with pytest.raises(PackageResolutionError, match="APPROVED_ASSIGNMENT_INVALID"):
        ParentPackageService(store).plan(request, approved_assignments=[{
            "document_ids": ["doc-1", "doc-2"], "manifest_path": "../arbitrary.xlsx"}],
            pending_assignments=[])


def test_byte_derived_semantics_reject_metadata_tamper_and_uom_conflicts():
    request, store = request_and_store()
    evidence_path = "candidates/request-1/candidate-1/validation/validation-1/evidence.json"
    evidence = json.loads(store.candidates[evidence_path])
    evidence["fixtures"][0]["semantic"]["fingerprint"] = "f" * 64
    store.candidates[evidence_path] = json.dumps(evidence).encode()
    package = plan(ParentPackageService(store), request)
    with pytest.raises(PackageResolutionError, match="PACKAGE_SEMANTIC_BINDING"):
        ParentPackageService(store).review(package["plan_id"])

    request, store = request_and_store(outputs=[
        package_workbook(uom="EA", assembly=("Board", "A")),
        package_workbook(uom="REEL", assembly=("Board", "A")),
    ])
    package = plan(ParentPackageService(store), request)
    with pytest.raises(PackageResolutionError, match="PACKAGE_CONFLICT_DISPOSITION_REQUIRED"):
        ParentPackageService(store).review(package["plan_id"])


def test_forged_assembly_identity_and_mapping_required_fail_closed():
    # Bytes carry no Document row, but the evidence claims an identity: forged.
    request, store = request_and_store(
        outputs=[package_workbook(assembly=None), package_workbook(assembly=None)],
        identities=[("", ""), ("", "")], claimed_identities=[("Board", "A"), ("Board", "A")])
    package = plan(ParentPackageService(store), request)
    with pytest.raises(PackageResolutionError, match="PACKAGE_SEMANTIC_BINDING"):
        ParentPackageService(store).review(package["plan_id"])

    # Bytes carry no Document row and nothing is claimed: mapping required.
    request, store = request_and_store(
        outputs=[package_workbook(assembly=None), package_workbook(assembly=None)],
        identities=[("", ""), ("", "")], claimed_identities=[None, None])
    package = plan(ParentPackageService(store), request)
    with pytest.raises(PackageResolutionError, match="PACKAGE_MAPPING_REQUIRED"):
        ParentPackageService(store).review(package["plan_id"])
    # An explicit mapping satisfies the requirement and binds to the fixture.
    review = ParentPackageService(store).review(package["plan_id"], assembly_mappings={
        request.documents[0].sha256: {"name": "Board", "revision": "A"},
        request.documents[1].sha256: {"name": "Board", "revision": "A"},
    })
    assert review["approval_ready"]
    assert review["groups"][0]["mapping_keys"] == sorted(
        [request.documents[0].sha256, request.documents[1].sha256])
    # A mapping for a fixture whose bytes already carry identity is rejected.
    request, store = request_and_store()
    package = plan(ParentPackageService(store), request)
    with pytest.raises(PackageResolutionError, match="PACKAGE_REVIEW_INPUT_INVALID"):
        ParentPackageService(store).review(package["plan_id"], assembly_mappings={
            request.documents[0].sha256: {"name": "Board", "revision": "A"}})


def test_candidate_code_evidence_is_verified_against_review_reference():
    request, store = request_and_store()
    service = ParentPackageService(store)
    package = plan(service, request)
    review = service.review(package["plan_id"])
    evidence = review["groups"][0]["evidence"]
    assert evidence["children"][0]["candidate"] == {
        "blob_path": "candidates/request-1/candidate-1/parse.py",
        "sha256": CODE_SHA, "size_bytes": len(CODE)}
    # Tampering the candidate code bytes must break the review binding.
    store.candidates["candidates/request-1/candidate-1/parse.py"] = b"# tampered\n"
    with pytest.raises(PackageResolutionError, match="PACKAGE_CANDIDATE_BINDING"):
        service.review(package["plan_id"])
    # A forged candidate reference (wrong hash) must fail closed.
    request, store = request_and_store()
    evidence_path = "candidates/request-1/candidate-1/validation/validation-1/evidence.json"
    value = json.loads(store.candidates[evidence_path])
    value["candidate"]["sha256"] = "f" * 64
    store.candidates[evidence_path] = json.dumps(value).encode()
    package = plan(ParentPackageService(store), request)
    with pytest.raises(PackageResolutionError, match="PACKAGE_CANDIDATE_BINDING"):
        ParentPackageService(store).review(package["plan_id"])


def test_complementary_mpns_merge_and_conflict_disposition_is_fixture_specific():
    request, store = request_and_store()
    service = ParentPackageService(store)
    package = plan(service, request)
    review = service.review(package["plan_id"])
    assert review["groups"][0]["evidence"]["reconciliation"] == "merged"
    _, approval = approve(service, package)
    resolved = service.resolve(plan_id=package["plan_id"], approval_id=approval["approval_id"])
    output = store.outputs[resolved["results"][0]["output"]["blob_path"]]
    frame = pd.read_excel(io.BytesIO(output), engine="openpyxl")
    row = frame.loc[frame["item_number"] == "R1"].iloc[0]
    assert {int(row["manufacturer_item_number_1"]),
            int(row["manufacturer_item_number_2"])} == {100, 200}

    request, store = request_and_store(outputs=[
        package_workbook(quantity=2, assembly=("Board", "A")),
        package_workbook(quantity=3, assembly=("Board", "A")),
    ])
    service = ParentPackageService(store)
    package = plan(service, request)
    selected = request.documents[0].sha256
    group_id = hashlib.sha256(json.dumps({"assembly_identity": ["board", "a"]},
        sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    review = service.review(package["plan_id"], conflict_dispositions={group_id: selected})
    assert review["groups"][0]["evidence"]["conflict_disposition"] == selected
    # A disposition naming a nonexistent fixture is rejected.
    with pytest.raises(PackageResolutionError, match="PACKAGE_CONFLICT_DISPOSITION_REQUIRED"):
        service.review(package["plan_id"], conflict_dispositions={group_id: "f" * 64})


def test_workbook_serialization_is_deterministic_and_replay_is_exact():
    left = ParentPackageService._workbook([{"item_number": "R1", "quantity": 2}])
    right = ParentPackageService._workbook([{"item_number": "R1", "quantity": 2}])
    assert left == right
    assert ParentPackageService._workbook([{"item_number": "R2", "quantity": 2}]) != left

    request, store = request_and_store()
    service = ParentPackageService(store)
    package = plan(service, request)
    _, approval = approve(service, package)
    first = service.resolve(plan_id=package["plan_id"], approval_id=approval["approval_id"])
    second = service.resolve(plan_id=package["plan_id"], approval_id=approval["approval_id"])
    assert first == second
    assert first["version"] == "resolved-output-manifest.v1"
    assert first["results"][0]["reconciliation"] == "merged"


def test_partial_output_replay_and_manifest_tamper_fail_closed():
    request, store = request_and_store()
    service = ParentPackageService(store)
    package = plan(service, request)
    _, approval = approve(service, package)
    original = store.upload_output_immutable
    calls = 0
    def fail_before_manifest(path, data, content_type):
        nonlocal calls
        calls += 1
        original(path, data, content_type)
        if calls == 1:
            raise RuntimeError("injected")
    store.upload_output_immutable = fail_before_manifest
    with pytest.raises(RuntimeError, match="injected"):
        service.resolve(plan_id=package["plan_id"], approval_id=approval["approval_id"])
    store.upload_output_immutable = original
    manifest = service.resolve(plan_id=package["plan_id"], approval_id=approval["approval_id"])
    manifest_path = (f"packages/{package['plan_id']}/resolved/{approval['approval_id']}/"
                     "resolved-output-manifest.v1.json")
    tampered = dict(manifest); tampered["results"] = []
    store.outputs[manifest_path] = json.dumps(tampered).encode()
    with pytest.raises(PackageResolutionError, match="RESOLVED_OUTPUT_CONFLICT"):
        service.resolve(plan_id=package["plan_id"], approval_id=approval["approval_id"])


def test_resolved_manifest_rejects_unknown_nested_output_fields():
    request, store = request_and_store()
    service = ParentPackageService(store)
    package = plan(service, request)
    _, approval = approve(service, package)
    manifest = service.resolve(plan_id=package["plan_id"], approval_id=approval["approval_id"])
    manifest["results"][0]["output"]["unexpected"] = "value"
    body = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    manifest["manifest_sha256"] = hashlib.sha256(json.dumps(
        body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    manifest_path = (f"packages/{package['plan_id']}/resolved/{approval['approval_id']}/"
                     "resolved-output-manifest.v1.json")
    store.outputs[manifest_path] = json.dumps(manifest).encode()
    with pytest.raises(PackageResolutionError, match="RESOLVED_OUTPUT_CONFLICT"):
        service.manifest(package["plan_id"], approval["approval_id"])
    # Tampering a published output blob also fails replay.
    request, store = request_and_store()
    service = ParentPackageService(store)
    package = plan(service, request)
    _, approval = approve(service, package)
    manifest = service.resolve(plan_id=package["plan_id"], approval_id=approval["approval_id"])
    store.outputs[manifest["results"][0]["output"]["blob_path"]] = b"tampered"
    with pytest.raises(PackageResolutionError, match="RESOLVED_OUTPUT_CONFLICT"):
        service.resolve(plan_id=package["plan_id"], approval_id=approval["approval_id"])


def test_manifest_and_resolution_status_read_state_without_side_effects():
    request, store = request_and_store()
    service = ParentPackageService(store)
    package = plan(service, request)
    _, approval = approve(service, package)
    # Before resolution: status is read-only and manifest is absent.
    status = service.resolution_status(package["plan_id"], approval["approval_id"])
    assert status == {"plan_id": package["plan_id"], "approval_id": approval["approval_id"],
                      "approval": approval, "resolved": False, "status": "approved"}
    with pytest.raises(PackageResolutionError, match="RESOLVED_OUTPUT_ABSENT"):
        service.manifest(package["plan_id"], approval["approval_id"])
    assert store.outputs == {}
    # After resolution: both read the published state and never re-publish.
    resolved = service.resolve(plan_id=package["plan_id"], approval_id=approval["approval_id"])
    assert service.manifest(package["plan_id"], approval["approval_id"]) == resolved
    assert service.resolution_status(package["plan_id"], approval["approval_id"])["status"] == "resolved"
    writes = []
    original = store.upload_output_immutable
    def tracking(path, data, content_type):
        writes.append(path)
        original(path, data, content_type)
    store.upload_output_immutable = tracking
    service.manifest(package["plan_id"], approval["approval_id"])
    service.resolution_status(package["plan_id"], approval["approval_id"])
    assert writes == []
    # Unknown approval id reports unknown without raising.
    unknown = service.resolution_status(package["plan_id"], "f" * 64)
    assert unknown["status"] == "unknown" and unknown["approval"] is None


def api_config():
    return ApiConfig("127.0.0.1", 0, "easy_auth", frozenset(), frozenset({"admin-1"}),
                     100_000, 1, {})


def auth_headers(oid="admin-1"):
    value = {"claims": [{"typ": "oid", "val": oid}]}
    message = Message()
    message["X-MS-CLIENT-PRINCIPAL"] = base64.b64encode(json.dumps(value).encode()).decode()
    return message


def test_package_api_uses_authenticated_actor_and_rejects_impersonation():
    class Service:
        def approve(self, **value):
            assert value["approver"] == "admin-1"
            return {"approval_id": "approval-1", "approver": value["approver"]}
    api = PackageApi(api_config(), Service)
    body = json.dumps({"review_manifest_sha256": "a" * 64}).encode()
    assert api.handle_post(auth_headers(), "plan-1", "approve", body)[1]["approver"] == "admin-1"
    status, response = api.handle_post(auth_headers(), "plan-1", "approve",
        json.dumps({"review_manifest_sha256": "a" * 64, "approver": "attacker"}).encode())
    assert status == 422 and response["error"]["code"] == "invalid_request"
    assert api.handle_post(auth_headers("other"), "plan-1", "approve", body)[0] == 403


def test_package_api_get_routes_are_read_only():
    class Service:
        def __init__(self):
            self.calls = []
        def review(self, plan_id):
            self.calls.append(("review", plan_id))
            return {"version": "package-review.v1", "plan_id": plan_id}
        def manifest(self, plan_id, approval_id):
            self.calls.append(("manifest", plan_id, approval_id))
            return {"version": "resolved-output-manifest.v1"}
        def resolution_status(self, plan_id, approval_id):
            self.calls.append(("status", plan_id, approval_id))
            return {"status": "resolved"}
    service = Service()
    api = PackageApi(api_config(), lambda: service)
    assert api.handle_get(auth_headers(), "plan-1", "status")[1]["plan_id"] == "plan-1"
    assert api.handle_get(auth_headers(), "plan-1", "review")[1]["plan_id"] == "plan-1"
    assert api.handle_get(auth_headers(), "plan-1", "manifest", "approval-1")[1]["version"] == "resolved-output-manifest.v1"
    assert api.handle_get(auth_headers(), "plan-1", "status", "approval-1")[1]["status"] == "resolved"
    assert service.calls == [("review", "plan-1"), ("review", "plan-1"),
                             ("manifest", "plan-1", "approval-1"),
                             ("status", "plan-1", "approval-1")]
    # Unknown actions are rejected without touching the service.
    assert api.handle_get(auth_headers(), "plan-1", "output")[0] == 405
    assert api.handle_get(auth_headers(), "plan-1", "resolve")[0] == 405
    assert len(service.calls) == 4
