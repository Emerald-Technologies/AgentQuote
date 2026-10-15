"""Package children reuse an approved registry parser before any model authoring.

A package BOM whose layout matches one approved variant still becomes a child candidate request (the
package review sees every BOM the same way); when the child is authored, the approved parser is
written as its candidate through the normal authoring boundary, selected, and recorded with its
registry provenance; the normal validator then takes it to awaiting approval. No match, several
matches, or sources matching different variants keep the model-authoring path. Synthetic data
only; no Azure or model calls.
Run: python -m pytest tests/test_registry_reuse.py
"""
import hashlib
import json

import pytest

from bom_processor.candidate_automation import CandidateAutomationService
from bom_processor.candidate_status import CandidateStatusService
from bom_processor.candidate_validation import CandidateValidationService
from bom_processor.candidates import AggregateCandidateRequiredError, persist_candidate
from bom_processor.core import _json_bytes
from bom_processor.master_registry import ApprovalRecord, ApprovedVariant, VariantSignature, profile_bytes
from bom_processor.operator_candidate import OfflineWorker, selection_path
from bom_processor.package_resolution import ParentPackageService
from bom_processor.registry_reuse import REUSE_ACTOR, match_request, reuse_path
from test_bom_package_choice import Agent, MOD, PackageChoiceService, Starter, make_request, remote, store
from test_operator_candidate import GOOD, NOW
from test_operator_promotion import package, promote, validated


def install(storage, variant_id, code, data, parser_key="Parser1", customer="cust", generation=None):
    """Write one approved variant exactly as AdminDecisionService publishes it."""
    signature = VariantSignature.from_profile(profile_bytes(data, ".xlsx").to_dict(), heading=True)
    code = code.encode() if isinstance(code, str) else code
    sha = hashlib.sha256(code).hexdigest()
    approval_id = hashlib.sha256(variant_id.encode()).hexdigest()
    prefix = f"approved/{parser_key}/{variant_id}/1"
    approval = ApprovalRecord(approval_id, "c" * 64, parser_key, customer, variant_id, "1", "d" * 64, sha,
                              signature.signature_hash, signature.rule_hash, "e" * 64, "nbuck",
                              "2026-10-14T00:00:00Z")
    storage.registry[f"{prefix}/parse.py"] = code
    storage.registry[f"{prefix}/signature.json"] = _json_bytes(signature.to_dict())
    storage.registry[f"{prefix}/approval.json"] = _json_bytes(approval.to_dict())
    variant = ApprovedVariant(parser_key, variant_id, "1", f"{prefix}/parse.py", sha, signature.signature_hash,
                              signature.rule_hash, approval_id)
    path = f"registry/{parser_key}/master.json"
    master = json.loads(storage.registry[path]) if path in storage.registry else {
        "schema": "master-registry.v1", "parser_key": parser_key, "generation": 0, "variants": []}
    master["variants"].append({**variant.to_dict(), "signature_path": f"{prefix}/signature.json",
                               "approval_path": f"{prefix}/approval.json"})
    master["generation"] += 1
    storage.registry[path] = _json_bytes(master)
    return variant


def automation(storage):
    return CandidateAutomationService(storage, Starter(), Starter(), author_budget_enabled=True,
                                      budget_clock=lambda: NOW)


def test_a_matched_package_bom_becomes_a_child_that_reuses_the_approved_parser():
    storage = store()
    service = PackageChoiceService(storage, Starter())
    variant = install(storage, "clean-mod", GOOD, MOD)
    processor = remote(storage, service)
    with pytest.raises(Exception):
        processor.process(make_request())  # package choice pending
    service.run(json.loads(next(v for k, v in storage.registry.items() if k.endswith("/request.json")
                                and k.startswith("package-choice/")))["request_id"], Agent())
    with pytest.raises(AggregateCandidateRequiredError) as required:
        processor.process(make_request())
    # The registry match did not bypass the package: one child request covers both kept BOMs.
    (child,) = required.value.candidates
    assert sorted(child.document_ids) == ["doc-mod", "doc-other"]
    plan = json.loads(storage.registry[f"packages/{required.value.plan_id}/package-plan.v1.json"])
    assert {item["kind"] for item in plan["assignments"]} == {"candidate_request", "excluded"}
    starter = Starter()
    service_automation = CandidateAutomationService(storage, starter, author_budget_enabled=True,
                                                    budget_clock=lambda: NOW)
    assert service_automation.start_author(child.request_id) == "registry_reused"
    assert starter.started == []  # no model job
    status = CandidateStatusService(storage).get(child.request_id)
    assert status["status"] == "generated_unvalidated" and status["code_hash"] == variant.code_sha256
    record = json.loads(storage.registry[reuse_path(child.request_id, status["candidate_id"])])
    assert (record["variant_id"], record["approval_id"], record["registry_generation"]) == (
        "clean-mod", variant.approval_id, 1)
    selection = json.loads(storage.registry[selection_path(child.request_id, status["candidate_id"])])
    assert selection["actor"] == REUSE_ACTOR
    # A replay neither re-authors nor writes a second selection.
    assert service_automation.start_author(child.request_id) is None


def test_a_promoted_parser_is_reused_and_validated_for_the_next_package():
    storage, (first,), _ = package()
    # The next quote's child request for the same files, created as a package plan creates it.
    request = json.loads(storage.registry[f"candidates/requests/{first}.json"])
    validated(storage, first)
    promote(storage, first)
    profile = profile_bytes(storage.sources[request["source_evidence"][0]["blob_path"]], ".xlsx")
    evidence = tuple({**item, "document_id": "next-" + item["document_id"]} for item in request["source_evidence"])
    child = persist_candidate(storage, "Parser1", "cust", profile, evidence, lambda: NOW, stable_order=True)
    assert child.request_id != first
    assert automation(storage).start_author(child.request_id) == "registry_reused"
    status = CandidateStatusService(storage).get(child.request_id)
    result = CandidateValidationService(child.request_id, status["candidate_id"], storage,
                                        OfflineWorker(storage)).validate()
    assert result.status == "awaiting_approval"
    assert CandidateStatusService(storage).get(child.request_id)["status"] == "awaiting_approval"


def _child(storage, data_by_doc):
    evidence, profile = [], None
    for doc, data in data_by_doc.items():
        path = f"cust/rfq-12345678-1234-4234-8234-123456789012/package-1/original-boms/{doc}.xlsx"
        storage.sources[path] = data
        evidence.append({"document_id": doc, "blob_path": path, "size_bytes": len(data),
                         "sha256": hashlib.sha256(data).hexdigest()})
        profile = profile or profile_bytes(data, ".xlsx")
    return persist_candidate(storage, "Parser1", "cust", profile, tuple(evidence), lambda: NOW)


def test_no_match_keeps_the_authoring_path():
    from test_operator_candidate import workbook
    from test_fixed_layout_parser import clean_rows
    storage = store()
    install(storage, "clean-mod", GOOD, MOD)
    child = _child(storage, {"doc-a": workbook(clean_rows(2))})
    selection, outcome = match_request(storage, json.loads(storage.registry[f"candidates/requests/{child.request_id}.json"]))
    assert selection is None and outcome == "parser_format_not_recognized"
    assert automation(storage).start_author(child.request_id) == "started"
    assert not [path for path in storage.registry if path.startswith("candidates/registry-reuse/")]


def test_several_matches_keep_the_authoring_path():
    storage = store()
    install(storage, "clean-a", GOOD, MOD)
    install(storage, "clean-b", GOOD + "\n", MOD)
    child = _child(storage, {"doc-a": MOD})
    selection, outcome = match_request(storage, json.loads(storage.registry[f"candidates/requests/{child.request_id}.json"]))
    assert selection is None and outcome == "parser_format_ambiguous"
    assert automation(storage).start_author(child.request_id) == "started"


def test_sources_matching_different_variants_keep_the_authoring_path():
    from test_operator_candidate import workbook
    from test_fixed_layout_parser import clean_rows
    storage = store()
    one, three = workbook(clean_rows(1)), workbook(clean_rows(3))
    install(storage, "clean-1", GOOD, one)
    install(storage, "clean-3", GOOD, three)
    child = _child(storage, {"doc-a": one, "doc-b": three})
    selection, outcome = match_request(storage, json.loads(storage.registry[f"candidates/requests/{child.request_id}.json"]))
    assert selection is None and outcome == "sources_match_different_variants"
    assert automation(storage).start_author(child.request_id) == "started"


def test_without_a_registry_the_package_plan_is_unchanged():
    storage = store()
    service = PackageChoiceService(storage, Starter())
    processor = remote(storage, service)
    with pytest.raises(Exception):
        processor.process(make_request())
    service.run(json.loads(next(v for k, v in storage.registry.items() if k.endswith("/request.json")
                                and k.startswith("package-choice/")))["request_id"], Agent())
    with pytest.raises(AggregateCandidateRequiredError) as required:
        processor.process(make_request())
    reference = required.value.plan_id
    other = store()
    install(other, "clean-mod", GOOD, MOD)
    service = PackageChoiceService(other, Starter())
    processor = remote(other, service)
    with pytest.raises(Exception):
        processor.process(make_request())
    service.run(json.loads(next(v for k, v in other.registry.items() if k.endswith("/request.json")
                                and k.startswith("package-choice/")))["request_id"], Agent())
    with pytest.raises(AggregateCandidateRequiredError) as required:
        processor.process(make_request())
    assert required.value.plan_id == reference
    assert ParentPackageService(other).review(reference)["groups"]
