"""Offline reviewer-agent contract (policy foundry.v5): no live model, Azure or customer data."""
import json
from types import SimpleNamespace

import pytest

from bom_processor.package_evidence_contracts import PackageEvidenceError, json_bytes, object_hash
from bom_processor.package_evidence_jobs import evidence_request
from bom_processor.package_evidence_foundry import FoundryEvidenceProposalModel, REVIEW_PROMPT_SHA256, parse_review_response
from bom_processor.package_evidence_review import REVIEW_INSTRUCTION
from test_package_evidence_jobs import setup
from test_package_evidence_retention import RetentionModel, retained_inputs, source_proof

POLICY = {"producer": "test", "model": "fake", "policyVersion": "package-evidence-foundry.v5", "promptSha256": "d" * 64}


class ReviewedModel(RetentionModel):
    reviewer_identity = {"model": "fake", "promptSha256": "e" * 64}

    def __init__(self, change=None, fail=None):
        super().__init__()
        self.review_change, self.fail, self.review_packet = change, fail, None

    def review(self, packet):
        self.review_packet = packet
        if self.fail:
            raise self.fail
        out = {"claims": [{"claimId": c["claimId"], "verdict": "agree", "reason": "ok",
                           "explanation": "Consistent with the file contents.", "quotes": []} for c in packet["claims"]],
               "findings": [], "verdict": "proceed"}
        if self.review_change:
            self.review_change(out, packet)
        return out


def service_and_request():
    service, _, storage, _ = setup()
    context, roots, books = retained_inputs()
    prefix = f'evidence-inputs/{context["qqUuid"]}/package-{context["packageVersion"]}/'
    refs, bookrefs = [], []
    for r in roots:
        path = prefix + r["documentId"] + "/" + r["sha256"]
        storage.sources[path] = r["data"]
        refs.append({k: r[k] for k in ("documentId", "sha256", "sizeBytes", "fileName")} | {"blobPath": path})
    for a in context["assemblies"]:
        bom = a["canonicalBom"]
        path = prefix + bom["documentId"] + "/" + bom["sha256"]
        storage.sources[path] = books[bom["documentId"]]
        bookrefs.append(bom | {"blobPath": path})
    request = evidence_request(context, refs, bookrefs, POLICY, source_proof(roots))
    service.submit(request)
    return service, request, storage


def test_v5_publishes_independent_review_without_producer_rationale_and_replays():
    service, request, storage = service_and_request()
    model = ReviewedModel()
    result = service.run(request["requestId"], model)
    assert set(result) == {"requestId", "status", "proposal", "classification", "manufacturingAssessment", "review"}
    review = result["review"]
    assert review["status"] == "completed" and review["verdict"] == "proceed"
    assert review["reviewSha256"] == object_hash({k: v for k, v in review.items() if k != "reviewSha256"})
    assert review["assessmentSha256"] == result["manufacturingAssessment"]["assessmentSha256"]
    packet = model.review_packet
    graph = result["proposal"]
    # Every association plus every non-retained exclusion is a claim; retained provenance is code-verified.
    retained = {r["evidenceId"] for r in model.packet["retainedProvenance"]}
    assert {c["claimId"] for c in packet["claims"] if c["kind"] == "link"} == {a["associationId"] for a in graph["associations"]}
    assert all(c["evidenceId"] not in retained for c in packet["claims"] if c["kind"] == "exclusion")
    blob = json_bytes(packet)
    for association in graph["associations"]:
        assert association["rationale"].encode() not in blob
    assert all("rationale" not in c and "citations" not in c for c in packet["claims"])
    assert any(f["readable"] and f["text"] for f in packet["files"])
    second = ReviewedModel()
    assert service.run(request["requestId"], second) == result and second.review_packet is None
    assert service.reconcile_publication(request["requestId"]) == result


class StrictReviewer(ReviewedModel):
    """Mimics the live reviewer (A025, 2026-10-06): any file it cannot read and that has no claim is
    reported as unaccounted unless the packet marks it as code-verified retained provenance."""

    def review(self, packet):
        out = super().review(packet)
        claimed = {c["evidenceId"] for c in packet["claims"]}
        for f in packet["files"]:
            if not f["readable"] and f["evidenceId"] not in claimed and not f["retainedKind"]:
                out["findings"].append({"kind": "unaccounted_file", "subject": f["fileName"],
                                        "explanation": "Present but unreadable and not accounted for.", "quotes": []})
        if out["findings"]:
            out["verdict"] = "stop"
        return out


def test_retained_bom_workbook_is_labeled_for_reviewer_and_does_not_stop_package():
    # Regression: A025 stopped because retained original BOM workbooks reached the reviewer as
    # unexplained unreadable files (readable false, no claim, no stated role).
    service, request, _ = service_and_request()
    model = StrictReviewer()
    review = service.run(request["requestId"], model)["review"]
    packet = model.review_packet
    assert packet["version"] == "package-evidence-review-packet.v2"
    retained = {r["evidenceId"]: r["kind"] for r in model.packet["retainedProvenance"]}
    files = {f["evidenceId"]: f for f in packet["files"]}
    assert "original_bom_input" in retained.values()
    for identity, kind in retained.items():
        assert files[identity]["retainedProvenance"] is True and files[identity]["retainedKind"] == kind
    assert all(f["retainedKind"] is None for i, f in files.items() if i not in retained)
    assert not any(f["kind"] == "unaccounted_file" and "original.xlsx" in f["subject"] for f in review["findings"])
    assert "original_bom_input" in REVIEW_INSTRUCTION and "never report them as unaccounted" in REVIEW_INSTRUCTION


def test_reviewer_prompt_allows_explicitly_shared_files_across_named_assemblies():
    # Nick, 2026-10-07: a file whose text states it applies to other named assemblies may be linked as shared.
    assert "except as shared" in REVIEW_INSTRUCTION and "shared scope" in REVIEW_INSTRUCTION


@pytest.mark.parametrize("fail, code", [(RuntimeError("provider body"), "REVIEW_REQUEST_FAILED"),
                                        (PackageEvidenceError("REVIEW_TIMEOUT"), "REVIEW_TIMEOUT")])
def test_reviewer_failure_preserves_proposal_and_publishes_failed_review(fail, code):
    service, request, storage = service_and_request()
    result = service.run(request["requestId"], ReviewedModel(fail=fail))
    assert result["status"] == "completed" and result["proposal"]
    assert result["review"]["status"] == "failed" and result["review"]["errorCode"] == code
    assert result["review"]["claims"] == [] and result["review"]["verdict"] == "needs_human"
    assert b"provider body" not in b"".join(storage.registry.values())


@pytest.mark.parametrize("change", [
    lambda out, packet: out["claims"].pop(),
    lambda out, packet: out["claims"].append(dict(out["claims"][0])),
    lambda out, packet: out["claims"][0].update(verdict="maybe"),
    lambda out, packet: out["claims"][0].update(claimId="f" * 64),
    lambda out, packet: out.update(extra=1),
    lambda out, packet: out["claims"][0]["quotes"].append({"evidenceId": "f" * 64, "text": "x"}),
    lambda out, packet: out.update(verdict="approve"),
])
def test_invalid_reviewer_output_is_failed_review_not_proceed(change):
    service, request, _ = service_and_request()
    review = service.run(request["requestId"], ReviewedModel(change=change))["review"]
    assert review["status"] == "failed" and review["errorCode"] == "REVIEW_OUTPUT_INVALID" and review["verdict"] == "needs_human"


def test_tampered_or_missing_review_fails_status_and_cannot_reexecute():
    service, request, storage = service_and_request()
    service.run(request["requestId"], ReviewedModel())
    path = service._path(request["requestId"], "review")
    review = json.loads(storage.registry[path])
    review["verdict"] = "stop"
    storage.registry[path] = json_bytes(review)
    with pytest.raises(PackageEvidenceError, match="REVIEW_RESULT_BINDING"):
        service.status(request["requestId"])


def test_v5_requires_reviewer_before_worker_claim():
    service, request, storage = service_and_request()
    with pytest.raises(PackageEvidenceError, match="REVIEWER_REQUIRED"):
        service.run(request["requestId"], RetentionModel())
    assert service._path(request["requestId"], "worker-claim") not in storage.registry


def test_foundry_reviewer_uses_fresh_client_tool_free_and_exact_prompt():
    seen, clients = [], []

    class Agent:
        def __init__(self, **kwargs):
            seen.append(kwargs)

        async def run(self, packet, *, options):
            # Reviewer replies are service-enforced JSON too (A026 had one producer call only).
            assert options["response_format"] == {"type": "json_object"} and options["store"] is False
            return SimpleNamespace(output_text=json.dumps({"claims": [], "findings": [], "verdict": "stop"}))

    model = FoundryEvidenceProposalModel("first", model_name="explicit", agent_factory=Agent,
                                         client_factory=lambda: clients.append(len(clients)) or f"client-{len(clients)}")
    assert model.review({"claims": []})["verdict"] == "stop"
    assert model.review({"claims": []})["verdict"] == "stop"
    assert [s["client"] for s in seen] == ["client-1", "client-2"]
    assert all(s["tools"] == [] and s["instructions"] == REVIEW_INSTRUCTION for s in seen)
    assert model.reviewer_identity == {"model": "explicit", "promptSha256": REVIEW_PROMPT_SHA256}


@pytest.mark.parametrize("value", [None, "", "[]", "```json\n{}\n```", '{"a":1,"a":2}', "{} x"])
def test_review_response_strict_json(value):
    with pytest.raises(PackageEvidenceError, match="REVIEW_OUTPUT_INVALID"):
        parse_review_response(value)
