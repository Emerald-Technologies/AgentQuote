import hashlib
import io
import json
from email.message import Message

import pandas as pd
import pytest

from bom_processor import (
    AggregateCandidateRequiredError,
    CandidateRequiredError,
    CandidateReviewService,
    CandidateStatusService,
    InMemoryStorage,
    RegistryError,
    unique_fixtures,
)
from bom_processor.admin_promotion import AdminDecisionService
from bom_processor.core import CANONICAL_COLUMNS, OUTPUT_CONTENT_TYPE
from bom_processor.semantic_facts import facts_from_xlsx
from deterministic_api import (
    ApiConfig,
    CandidateReviewOutputApi,
    DecisionApi,
    ProcessingApi,
)


def workbook(*, quantity=2, extra_mpn=None, assembly="BOARD-1"):
    rows = []
    if assembly:
        document = {name: None for name in CANONICAL_COLUMNS}
        document.update(line_number=1, item_number=assembly, item_name=assembly,
                        revision="A", item_category="Document", quantity=1)
        rows.append(document)
    row = {name: None for name in CANONICAL_COLUMNS}
    row.update(line_number=len(rows) + 1, item_number="R1", item_name="Resistor",
               item_category="Part", quantity=quantity,
               manufacturer_1="Acme", manufacturer_item_number_1="100")
    if extra_mpn:
        row.update(manufacturer_2="Other", manufacturer_item_number_2=extra_mpn)
    stream = io.BytesIO()
    rows.append(row)
    pd.DataFrame(rows, columns=CANONICAL_COLUMNS).to_excel(stream, index=False)
    return stream.getvalue()


def grouped_storage(*, second_quantity=2, second_extra_mpn=None, second_identity="BOARD-1"):
    request_id, candidate_id, validation_id = "request-1", "candidate-1", "validation-1"
    code = b"# parser\n"
    code_hash = hashlib.sha256(code).hexdigest()
    outputs = [workbook(), workbook(quantity=second_quantity, extra_mpn=second_extra_mpn,
                                    assembly=second_identity)]
    sources = [b"source-one", b"source-two"]
    source_items = []
    fixtures = []
    candidates = {
        f"candidates/{request_id}/{candidate_id}/parse.py": code,
        f"candidates/{request_id}/{candidate_id}/candidate-manifest.json": json.dumps({
            "version": "candidate-authoring.v3", "request_id": request_id,
            "candidate_id": candidate_id, "status": "generated_unvalidated",
            "code_sha256": code_hash,
        }).encode(),
    }
    source_blobs = {}
    for index, (source, output) in enumerate(zip(sources, outputs), 1):
        digest = hashlib.sha256(source).hexdigest()
        source_path = f"boms/doc-{index}.csv"
        output_path = (f"candidates/{request_id}/{candidate_id}/validation/{validation_id}/"
                       f"worker/output-{index}.xlsx")
        alias = {"document_id": f"doc-{index}", "blob_path": source_path}
        source_items.append({**alias, "size_bytes": len(source), "sha256": digest})
        source_blobs[source_path] = source
        candidates[output_path] = output
        identity = "BOARD-1" if index == 1 else second_identity
        facts = facts_from_xlsx(output)
        fixtures.append({
            "fixture_sha256": digest, "document_id": f"doc-{index}",
            "source_aliases": [alias], "status": "passed",
            "output": {"blob_path": output_path, "sha256": hashlib.sha256(output).hexdigest(),
                       "size_bytes": len(output), "content_type": OUTPUT_CONTENT_TYPE},
            "schema": {"valid": True},
            "semantic": {"valid": True, "result": "valid",
                         "fingerprint": facts.fingerprint,
                         "assembly_identity": ({"name": identity.casefold(), "revision": "a"}
                                               if identity else None),
                         "mapping_required": not identity,
                         "model_summary": facts.model_summary},
        })
    evidence_path = f"candidates/{request_id}/{candidate_id}/validation/{validation_id}/evidence.json"
    candidates[evidence_path] = json.dumps({
        "version": "candidate-validation.v3", "request_id": request_id,
        "candidate_id": candidate_id, "validation_id": validation_id,
        "status": "awaiting_approval", "complete": True, "fixture_count": 2,
        "candidate": {"sha256": code_hash}, "fixtures": fixtures,
        "policy_versions": [],
    }).encode()
    candidates[f"candidates/events/{request_id}/{candidate_id}/validated-{validation_id}.json"] = json.dumps({
        "validation_id": validation_id, "status": "awaiting_approval",
    }).encode()
    store = InMemoryStorage(sources=source_blobs, candidates=candidates)
    store.registry[f"candidates/requests/{request_id}.json"] = json.dumps({
        "request_id": request_id, "status": "requested", "parser_key": "Parser1",
        "customer_key": "customer-1", "profile_hash": "a" * 64,
        "profile": {"extension": ".csv"}, "source_evidence": source_items,
    }).encode()
    return store


def config():
    return ApiConfig("127.0.0.1", 0, "local_disabled", frozenset(), frozenset(), 100_000, 1, {})


def test_fixture_grouping_is_order_independent_and_deduplicates_aliases():
    shared = "a" * 64
    evidence = [
        {"document_id": "doc-2", "blob_path": "boms/two.csv", "sha256": shared, "size_bytes": 10},
        {"document_id": "doc-3", "blob_path": "boms/three.csv", "sha256": "b" * 64, "size_bytes": 12},
        {"document_id": "doc-1", "blob_path": "boms/one.csv", "sha256": shared, "size_bytes": 10},
    ]
    forward = unique_fixtures(evidence)
    reverse = unique_fixtures(list(reversed(evidence)))
    assert forward == reverse
    assert len(forward) == 2
    assert forward[0]["alias_count"] == 2
    assert forward[0]["source_aliases"] == [
        {"document_id": "doc-1", "blob_path": "boms/one.csv"},
        {"document_id": "doc-2", "blob_path": "boms/two.csv"},
    ]


def test_grouped_status_uses_complete_fixture_results_without_top_level_schema():
    store = grouped_storage()
    result = CandidateStatusService(store).get("request-1")
    assert result["status"] == "awaiting_approval"
    assert result["review"]["version"] == "candidate-review.v3"
    assert result["review"]["approval_ready"] is True
    path = "candidates/request-1/candidate-1/validation/validation-1/evidence.json"
    evidence = json.loads(store.candidates[path])
    evidence["fixtures"][1]["status"] = "failed"
    evidence["status"] = "generated_unvalidated"
    store.candidates[path] = json.dumps(evidence).encode()
    assert CandidateStatusService(store).get("request-1")["status"] == "schema_invalid"


def test_v3_semantic_equivalence_dominance_conflict_and_mapping_required():
    equivalent = CandidateReviewService(grouped_storage()).get_v3("request-1", "candidate-1", "validation-1")
    comparison = equivalent["groups"][0]["comparisons"][0]
    assert comparison["relation"] == "equivalent"
    assert len(comparison["equivalent_aliases"]) == 2
    assert equivalent["approval_ready"] is True

    dominant = CandidateReviewService(grouped_storage(second_extra_mpn="200")).get_v3(
        "request-1", "candidate-1", "validation-1")
    comparison = dominant["groups"][0]["comparisons"][0]
    assert comparison["relation"] == "dominates"
    assert comparison["selected_fixture_sha256"] == dominant["groups"][0]["selected_fixture_sha256"]

    conflict = CandidateReviewService(grouped_storage(second_quantity=3)).get_v3(
        "request-1", "candidate-1", "validation-1")
    assert conflict["groups"][0]["comparisons"][0]["relation"] == "conflict"
    assert conflict["groups"][0]["comparisons"][0]["material_differences"]
    assert conflict["approval_ready"] is False

    mapping = CandidateReviewService(grouped_storage(second_identity="")).get_v3(
        "request-1", "candidate-1", "validation-1")
    assert any(group["mapping_required"] for group in mapping["groups"])
    assert mapping["approval_ready"] is False


def test_grouped_approval_requires_manifest_and_validates_conflict_selection():
    store = grouped_storage(second_quantity=3)
    review = CandidateReviewService(store).get_v3("request-1", "candidate-1", "validation-1")
    manifest_hash = review["evidence"]["manifest_sha256"]
    group = review["groups"][0]
    common = dict(request_id="request-1", candidate_id="candidate-1", validation_id="validation-1",
                  decision="approve", approver={"object_id": "admin-1"}, reason="", ticket="",
                  variant_id="csv", version="1")
    service = AdminDecisionService(store)
    with pytest.raises(RegistryError, match="REVIEW_MANIFEST_BINDING"):
        service.decide(**common)
    with pytest.raises(RegistryError, match="FIXTURE_SELECTION_INVALID"):
        service.decide(**common, review_manifest_sha256=manifest_hash,
                       fixture_selections={group["group_id"]: "f" * 64})
    selected = group["fixtures"][0]["fixture_sha256"]
    result = service.decide(**common, review_manifest_sha256=manifest_hash,
                            fixture_selections={group["group_id"]: selected})
    assert result["review_manifest_sha256"] == manifest_hash
    decision = json.loads(store.registry["candidates/decisions/request-1/candidate-1/validation-1.json"])
    assert decision["fixture_selections"] == {group["group_id"]: selected}


def test_decision_api_echoes_review_manifest_hash():
    class Service:
        def decide(self, **request):
            assert request["review_manifest_sha256"] == "a" * 64
            return {"decision_id": "decision-1", "decision": "approve",
                    "candidate_id": "candidate-1", "validation_id": "validation-1",
                    "review_manifest_sha256": "a" * 64}
    api = DecisionApi(config(), Service)
    status, response = api.handle(Message(), json.dumps({
        "requestId": "request-1", "candidateId": "candidate-1",
        "validationId": "validation-1", "decision": "approve", "actorId": "admin-1",
        "variantId": "csv", "version": "1", "reviewManifestSha256": "a" * 64,
    }).encode())
    assert status == 200
    assert response["reviewManifestSha256"] == "a" * 64


def test_aggregate_candidate_response_starts_every_group_and_reports_parent_identity():
    errors = (CandidateRequiredError("request-1", "Parser1", "a" * 64, ("doc-1",)),
              CandidateRequiredError("request-2", "Parser1", "b" * 64, ("doc-2",)))
    class Processor:
        def process(self, request):
            raise AggregateCandidateRequiredError(errors)
    class Automation:
        calls = []
        def start_author(self, request_id):
            self.calls.append(request_id)
            if request_id == "request-2":
                raise RuntimeError("bounded")
            return "started"
    value = {"job_id": "job1", "customer_key": "cust", "parser_key": "Parser1",
             "rfq_uuid": "12345678-1234-4234-8234-123456789012", "package_version": "v1",
             "documents": [{"document_id": "doc1", "blob_path": "cust/rfq-12345678-1234-4234-8234-123456789012/package-v1/original-boms/doc1.csv",
                            "size_bytes": 1, "sha256": "0" * 64}]}
    automation = Automation()
    status, response = ProcessingApi(config(), Processor, lambda: automation).handle(
        Message(), json.dumps(value).encode())
    assert status == 202
    assert automation.calls == ["request-1", "request-2"]
    assert response["job_id"] == "job1" and len(response["request_fingerprint"]) == 64
    assert response["candidates"][1]["automation_error"] == "author_start_failed"


def test_aggregate_candidate_response_adds_parent_plan_id_when_available():
    errors = (CandidateRequiredError("request-1", "Parser1", "a" * 64, ("doc-1",)),
              CandidateRequiredError("request-2", "Parser1", "b" * 64, ("doc-2",)))
    class Processor:
        def process(self, request):
            raise AggregateCandidateRequiredError(errors, "c" * 64)
    value = {"job_id": "job1", "customer_key": "cust", "parser_key": "Parser1",
             "rfq_uuid": "12345678-1234-4234-8234-123456789012", "package_version": "v1",
             "documents": [{"document_id": "doc1", "blob_path": "cust/rfq-12345678-1234-4234-8234-123456789012/package-v1/original-boms/doc1.csv",
                            "size_bytes": 1, "sha256": "0" * 64}]}
    status, response = ProcessingApi(config(), Processor).handle(Message(), json.dumps(value).encode())
    assert status == 202 and response["plan_id"] == "c" * 64


def test_fixture_output_endpoint_rechecks_bytes_after_review():
    store = grouped_storage()
    review = CandidateReviewService(store).get_v3("request-1", "candidate-1", "validation-1")
    fixture = review["groups"][0]["fixtures"][0]
    original = store.download_candidate
    calls = {fixture["output"]["blob_path"]: 0}
    def changing_download(path, max_bytes):
        if path in calls:
            calls[path] += 1
            if calls[path] == 2:
                return b"tampered"
        return original(path, max_bytes)
    store.download_candidate = changing_download
    status, response = CandidateReviewOutputApi(config(), lambda: CandidateReviewService(store)).handle_fixture(
        Message(), "request-1", "candidate-1", "validation-1", fixture["fixture_sha256"])
    assert status == 409
    assert response["error"]["code"] == "review_unavailable"
