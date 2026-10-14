"""Offline classification contract, advisory MPN references and immutable publication."""
from copy import deepcopy

import pytest

from bom_processor.package_evidence_contracts import PackageEvidenceError, json_bytes, object_hash
from bom_processor.package_evidence_jobs import evidence_request
from bom_processor.package_evidence_proposal import produce_proposal
from bom_processor.package_part_classification import advisory_signals
from test_package_evidence_proposal import FakeModel, inputs
from test_package_evidence_jobs import setup


class ClassifiedModel(FakeModel):
    def propose(self, packet):
        output = super().propose(packet)
        output["classifications"] = [{"occurrenceId": o["occurrenceId"],
            "partType": "commercial", "rationale": "Offline judgment, not engineering approval",
            "evidenceIds": [], "sourceRequiresDrawing": False, "sourceRequiresRevision": False,
            "conflicts": []} for o in packet["occurrences"]]
        return output


def produce(model=None):
    context, roots, books = inputs()
    return produce_proposal(context, roots, books, model=model or ClassifiedModel(),
        producer="offline-test", model_name="fake-model", policy_version="package-evidence-foundry.v2",
        prompt_sha256="d" * 64, run_id="test-run", include_classification=True)


def test_complete_artifact_and_advisory_signal_cannot_override_model():
    model = ClassifiedModel()
    result = produce(model)
    artifact = result["classification"]
    assert artifact["proposalSha256"] == result["proposal"]["proposalSha256"]
    assert artifact["classificationSha256"] == object_hash(
        {k: v for k, v in artifact.items() if k != "classificationSha256"})
    assert len(artifact["classifications"]) == 4
    assert all(c["partType"] == "commercial" for c in artifact["classifications"])
    rows = model.packet["verifiedRows"]
    assert len(rows) == 6 and sum(r["occurrenceId"] is None for r in rows) == 2
    for c in artifact["classifications"]:
        assert c["rowObservationSha256"] == object_hash(next(r for r in rows if r["occurrenceId"] == c["occurrenceId"]))
    assert "classificationSha256" not in result["proposal"]


def test_mpn_hints_missing_equal_mixed_and_punctuation():
    cells = [None] * 25
    cells[1] = "CP-1"
    row = {"cells": cells}
    assert advisory_signals(row)[0]["rule"] == "missing-mpn"
    cells[24] = " cp-1 "
    assert advisory_signals(row)[0]["rule"] == "mpn-equals-customer-part-number"
    cells[10] = "OTHER-MPN"
    signals = advisory_signals(row)
    assert len(signals) == 1 and signals[0]["suggestion"] == "noncommercial"
    cells[24] = "CP1"
    assert advisory_signals(row) == []
    cells[2] = "PCB custom"
    assert {s["suggestion"] for s in advisory_signals(row)} == {"pcb", "custom"}
    cells[2] = None
    cells[5] = "PCB"
    assert advisory_signals(row) == []  # Unit of measure is not notes.
    cells[8] = "fabricated"
    assert advisory_signals(row)[0]["field"] == "notes"


@pytest.mark.parametrize("change", [
    lambda c: c.pop(), lambda c: c.append(deepcopy(c[0])),
    lambda c: c[0].update(occurrenceId="0" * 64),
    lambda c: c[0].update(partType="document"),
    lambda c: c[0].update(sourceRequiresDrawing=0),
    lambda c: c[0].update(evidenceIds=["0" * 64]),
    lambda c: c[0].update(rowObservationSha256="0" * 64),
])
def test_incomplete_foreign_or_model_owned_binding_rejected(change):
    class Invalid(ClassifiedModel):
        def propose(self, packet):
            value = super().propose(packet)
            change(value["classifications"])
            return value
    with pytest.raises(PackageEvidenceError):
        produce(Invalid())


def classified_service():
    service, original, storage, _ = setup()
    policy = {**original["policy"], "policyVersion": "package-evidence-foundry.v2"}
    request = evidence_request(original["context"], original["roots"], original["workbooks"], policy)
    service.submit(request)
    return service, request, storage


def test_classified_job_persisted_replayed_and_tampering_rejected():
    service, request, storage = classified_service()
    result = service.run(request["requestId"], ClassifiedModel())
    assert result["classification"]["runId"] == request["requestId"]
    second = ClassifiedModel()
    assert service.run(request["requestId"], second) == result and second.packet is None
    path = service._path(request["requestId"], "classification")
    artifact = deepcopy(result["classification"])
    artifact["classifications"][0]["partType"] = "unknown"
    storage.registry[path] = json_bytes(artifact)
    with pytest.raises(PackageEvidenceError, match="CLASSIFICATION_RESULT_BINDING"):
        service.status(request["requestId"])


def test_classified_recovery_requires_both_artifacts_no_reexecution():
    service, request, storage = classified_service()
    original = storage.upload_registry_immutable
    def interrupt(path, data, content_type):
        if path.endswith("/classification.json"):
            raise RuntimeError("interrupted publication")
        original(path, data, content_type)
    storage.upload_registry_immutable = interrupt
    with pytest.raises(PackageEvidenceError, match="PUBLICATION_UNKNOWN"):
        service.run(request["requestId"], ClassifiedModel())
    storage.upload_registry_immutable = original
    with pytest.raises(PackageEvidenceError, match="CLASSIFICATION_RESULT_MISSING"):
        service.reconcile_publication(request["requestId"])
    model = ClassifiedModel()
    assert service.run(request["requestId"], model)["status"] == "running" and model.packet is None


def test_aggregate_classification_output_rejected_before_publication():
    class Oversized(ClassifiedModel):
        def propose(self, packet):
            value = super().propose(packet)
            value["classifications"] = [{"rationale": "x" * (8 * 1024 * 1024)}]
            return value
    service, request, storage = classified_service()
    with pytest.raises(PackageEvidenceError, match="EVIDENCE_JOB_FAILED"):
        service.run(request["requestId"], Oversized())
    assert service._path(request["requestId"], "proposal") not in storage.registry
    assert service._path(request["requestId"], "classification") not in storage.registry


def test_new_adapter_refuses_legacy_policy_before_claim_or_invocation():
    service, request, storage, _ = setup()
    service.submit(request)
    class NewAdapter(ClassifiedModel):
        policy_version = "package-evidence-foundry.v2"
    model = NewAdapter()
    with pytest.raises(PackageEvidenceError, match="MODEL_POLICY_BINDING"):
        service.run(request["requestId"], model)
    assert model.packet is None
    assert service.status(request["requestId"])["status"] == "requested"
    assert service._path(request["requestId"], "worker-claim") not in storage.registry