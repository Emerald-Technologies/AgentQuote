"""Bounded successor requests: offline storage/model only, no Azure or customer data."""
from copy import deepcopy

import pytest

from bom_processor.package_evidence_contracts import PackageEvidenceError
from bom_processor.package_evidence_jobs import evidence_request
from bom_processor.package_evidence_successor import MAX_ATTEMPT, validate_successor
from test_package_evidence_jobs import setup
from test_package_evidence_retention import RetentionModel, retained_inputs, source_proof

PARENT = "a" * 64
FEEDBACK = {"qualificationSha256": "b" * 64, "findings": [
    {"kind": "sharing", "subject": "c" * 64, "reasonCode": "ENGINEERING_SHARING_SOURCE_REFERENCE_UNPROVEN",
     "evidenceIds": ["d" * 64], "association": {"evidenceId": "d" * 64,
        "assemblyUuids": ["12345678-1234-4234-8234-123456789012"], "scope": "shared"}},
    {"kind": "identity_revision", "subject": "12345678-1234-4234-8234-123456789012",
     "reasonCode": "ENGINEERING_PART_NUMBER_CONFLICT", "evidenceIds": [], "association": None}]}


def successor(attempt=2, parent=PARENT, feedback=None):
    return {"predecessorProposalSha256": parent, "attempt": attempt, "feedback": deepcopy(feedback or FEEDBACK)}


def staged():
    service, _, storage, starter = setup()
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
    policy = {"producer": "test", "model": "fake", "policyVersion": "package-evidence-foundry.v4", "promptSha256": "d" * 64}
    return service, storage, starter, (context, refs, bookrefs, policy, source_proof(roots))


def test_v3_successor_is_distinct_bound_and_feedback_reaches_packet_only():
    service, storage, starter, args = staged()
    initial = evidence_request(*args)
    request = evidence_request(*args, successor())
    assert request["version"] == "package-evidence-request.v3" and request["requestId"] != initial["requestId"]
    assert service.submit(request)["status"] == "requested"
    model = RetentionModel()
    result = service.run(request["requestId"], model)
    assert result["status"] == "completed"
    assert result["proposal"]["parentProposalSha256"] == PARENT
    assert result["proposal"]["provenance"]["runId"] == request["requestId"]
    assert model.packet["correctionFeedback"] == {"attempt": 2, "findings": FEEDBACK["findings"]}
    assert "Never fabricate support" in model.packet["correctionInstruction"]
    # Replay and reconciliation keep the predecessor binding without another model call.
    replay = RetentionModel()
    assert service.run(request["requestId"], replay) == result and replay.packet is None
    assert service.reconcile_publication(request["requestId"]) == result
    assert service.request(request["requestId"]) == request and not starter.calls


def test_initial_request_has_no_correction_and_rejects_successor_field():
    service, _, _, args = staged()
    initial = evidence_request(*args)
    model = RetentionModel()
    service.submit(initial)
    graph = service.run(initial["requestId"], model)["proposal"]
    assert graph["parentProposalSha256"] is None and "correctionFeedback" not in model.packet
    smuggled = deepcopy(initial)
    smuggled["successor"] = successor()
    with pytest.raises(PackageEvidenceError):
        service.submit(smuggled)


@pytest.mark.parametrize("change", [
    lambda s: s.update(attempt=1),
    lambda s: s.update(attempt=MAX_ATTEMPT + 1),
    lambda s: s.update(attempt=True),
    lambda s: s.update(predecessorProposalSha256="A" * 64),
    lambda s: s.update(extra=1),
    lambda s: s["feedback"].update(findings=[]),
    lambda s: s["feedback"]["findings"][0].update(reasonCode="free text"),
    lambda s: s["feedback"]["findings"][0].update(kind="model_opinion"),
    lambda s: s["feedback"]["findings"][0].update(sourceText="quoted customer content"),
    lambda s: s["feedback"]["findings"][0]["association"].update(scope="global"),
    lambda s: s["feedback"]["findings"][0].update(evidenceIds=["d" * 64, "d" * 64]),
])
def test_invalid_successor_rejected_before_storage(change):
    service, storage, starter, args = staged()
    value = successor()
    change(value)
    with pytest.raises(PackageEvidenceError):
        evidence_request(*args, value)
    assert not storage.registry and not starter.calls


def test_successor_requires_retained_policy_and_tamper_breaks_request_identity():
    _, _, _, (context, refs, bookrefs, policy, proof) = staged()
    legacy = {**policy, "policyVersion": "package-evidence-foundry.v3"}
    with pytest.raises(PackageEvidenceError, match="SUCCESSOR_POLICY_BINDING"):
        evidence_request(context, refs, bookrefs, legacy, None, successor())
    service, storage, _, args = staged()
    request = evidence_request(*args, successor())
    tampered = deepcopy(request)
    tampered["successor"]["attempt"] = 3
    with pytest.raises(PackageEvidenceError, match="REQUEST_CORRUPT"):
        service.submit(tampered)
    assert not storage.registry
    assert validate_successor(successor(5))["attempt"] == 5
