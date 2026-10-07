"""Failed-request retries: offline storage/model only, no Azure or customer data."""
from copy import deepcopy

import pytest

from bom_processor.package_evidence_contracts import PackageEvidenceError
from bom_processor.package_evidence_jobs import evidence_request
from bom_processor.package_evidence_retry import request_attempt, validate_retry
from test_package_evidence_retention import RetentionModel
from test_package_evidence_successor import staged, successor


class Failing(RetentionModel):
    def propose(self, packet):
        super().propose(packet)
        raise PackageEvidenceError("MODEL_RESPONSE_INVALID")


def failed_initial():
    service, storage, starter, args = staged()
    initial = evidence_request(*args)
    service.submit(initial)
    with pytest.raises(PackageEvidenceError, match="EVIDENCE_JOB_FAILED"):
        service.run(initial["requestId"], Failing())
    assert service.status(initial["requestId"])["status"] == "failed"
    return service, storage, starter, args, initial


def retry_of(request, attempt=2):
    return {"retryOfRequestId": request["requestId"], "attempt": attempt}


def test_retry_of_failed_request_is_new_identity_and_runs_like_initial():
    service, _, starter, args, initial = failed_initial()
    request = evidence_request(*args, retry=retry_of(initial))
    assert request["version"] == "package-evidence-request.v4"
    assert request["requestId"] != initial["requestId"]
    assert request["retry"] == {"retryOfRequestId": initial["requestId"], "attempt": 2}
    assert service.submit(request)["status"] == "requested"
    model = RetentionModel()
    result = service.run(request["requestId"], model)
    assert result["status"] == "completed"
    graph = result["proposal"]
    # No parent and no correction feedback: the model sees exactly what the initial run saw.
    assert graph["parentProposalSha256"] is None and graph["provenance"]["runId"] == request["requestId"]
    assert "correctionFeedback" not in model.packet and "correctionInstruction" not in model.packet
    assert service.request(request["requestId"]) == request and request_attempt(request) == 2
    # The failed predecessor stays failed and immutable.
    assert service.status(initial["requestId"])["status"] == "failed" and not starter.calls


def test_retry_chain_of_failures_increments_attempt():
    service, _, _, args, initial = failed_initial()
    second = evidence_request(*args, retry=retry_of(initial, 2))
    service.submit(second)
    with pytest.raises(PackageEvidenceError):
        service.run(second["requestId"], Failing())
    third = evidence_request(*args, retry=retry_of(second, 3))
    assert service.submit(third)["status"] == "requested"
    skipped = evidence_request(*args, retry=retry_of(second, 4))
    with pytest.raises(PackageEvidenceError, match="RETRY_LINEAGE_INVALID"):
        service.submit(skipped)


def test_retry_requires_existing_failed_predecessor():
    service, _, _, args = staged()
    initial = evidence_request(*args)
    with pytest.raises(PackageEvidenceError, match="RETRY_PREDECESSOR_NOT_FOUND"):
        service.submit(evidence_request(*args, retry=retry_of(initial)))
    service.submit(initial)
    with pytest.raises(PackageEvidenceError, match="RETRY_PREDECESSOR_NOT_FAILED"):
        service.submit(evidence_request(*args, retry=retry_of(initial)))
    service.run(initial["requestId"], RetentionModel())
    assert service.status(initial["requestId"])["status"] == "completed"
    with pytest.raises(PackageEvidenceError, match="RETRY_PREDECESSOR_NOT_FAILED"):
        service.submit(evidence_request(*args, retry=retry_of(initial)))


def test_retry_must_bind_identical_package_inputs_but_may_change_policy():
    service, _, _, (context, roots, books, policy, proof), initial = failed_initial()
    changed = deepcopy(roots)
    changed[0]["fileName"] = "renamed.xlsx" if changed[0]["fileName"].endswith(".xlsx") else "renamed.txt"
    with pytest.raises(PackageEvidenceError, match="RETRY_INPUT_MISMATCH"):
        service.submit(evidence_request(context, changed, books, policy, proof, retry=retry_of(initial)))
    newer = {**policy, "promptSha256": "e" * 64}
    assert service.submit(evidence_request(context, roots, books, newer, proof, retry=retry_of(initial)))["status"] == "requested"


def test_retry_of_successor_is_unsupported_and_fields_are_exclusive():
    service, _, _, args = staged()
    with pytest.raises(PackageEvidenceError, match="RETRY_POLICY_BINDING"):
        evidence_request(*args, successor(), retry={"retryOfRequestId": "a" * 64, "attempt": 3})
    v3 = evidence_request(*args, successor())
    service.submit(v3)
    with pytest.raises(PackageEvidenceError):
        service.run(v3["requestId"], Failing())
    with pytest.raises(PackageEvidenceError, match="RETRY_PREDECESSOR_UNSUPPORTED"):
        service.submit(evidence_request(*args, retry=retry_of(v3, 3)))
    context, roots, books, _, _ = args
    legacy = {"producer": "test", "model": "fake", "policyVersion": "v1", "promptSha256": "d" * 64}
    with pytest.raises(PackageEvidenceError, match="RETRY_POLICY_BINDING"):
        evidence_request(context, roots, books, legacy, retry={"retryOfRequestId": "a" * 64, "attempt": 2})


@pytest.mark.parametrize("value", [None, {}, {"retryOfRequestId": "a" * 64}, {"retryOfRequestId": "x", "attempt": 2},
    {"retryOfRequestId": "a" * 64, "attempt": 1}, {"retryOfRequestId": "a" * 64, "attempt": 6},
    {"retryOfRequestId": "a" * 64, "attempt": True}, {"retryOfRequestId": "a" * 64, "attempt": 2, "feedback": {}}])
def test_invalid_retry_binding(value):
    with pytest.raises(PackageEvidenceError):
        validate_retry(value)


def test_retry_field_cannot_be_smuggled_into_other_versions():
    service, _, _, args, initial = failed_initial()
    request = evidence_request(*args, retry=retry_of(initial))
    for version in ("package-evidence-request.v2", "package-evidence-request.v3"):
        tampered = {**deepcopy(request), "version": version}
        with pytest.raises(PackageEvidenceError):
            service.submit(tampered)
    stripped = deepcopy(request)
    del stripped["retry"]
    with pytest.raises(PackageEvidenceError):
        service.submit(stripped)
