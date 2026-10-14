"""Offline API/worker integration using existing real artifact service and fake model."""
import pytest

from bom_processor.package_evidence_api import EvidenceApi, read_evidence_body, configured_policy
from bom_processor.package_evidence_contracts import json_bytes, PackageEvidenceError
from evidence_job import run_worker
from test_package_evidence_jobs import setup
from test_package_evidence_proposal import FakeModel


def test_submit_start_status_worker_complete_no_approval():
    service, request, _, starter = setup()
    api = EvidenceApi(authorize=lambda _: None, service_factory=lambda: service, policy=request["policy"], enabled=True)
    identity = request["requestId"]
    assert api.handle({}, identity, "submit", json_bytes(request))[0] == 200
    assert api.handle({}, identity, "start", b"{}")[0] == 202
    assert api.handle({}, identity, "status")[1]["status"] == "started"
    model = FakeModel(); model.model_name = request["policy"]["model"]
    assert run_worker(service, identity, request["policy"], lambda: model)["status"] == "completed"
    status, result = api.handle({}, identity, "status")
    assert status == 200 and result["status"] == "completed" and len(starter.calls) == 1
    assert "approval" not in result


@pytest.mark.parametrize("exception,status", [(PermissionError, 401), (LookupError, 403)])
def test_auth_before_factory_or_payload(exception, status):
    def deny(_):
        raise exception
    def forbidden():
        pytest.fail("factory accessed")
    api = EvidenceApi(authorize=deny, service_factory=forbidden, policy={}, enabled=True)
    assert api.handle({}, "invalid", "submit", b"invalid")[0] == status


def test_disabled_no_factory():
    api = EvidenceApi(authorize=lambda _: None, service_factory=lambda: pytest.fail("factory"), policy={})
    assert api.handle({}, "invalid", "status")[0] == 503


def test_policy_rejection_no_request_or_start():
    service, request, storage, starter = setup()
    api = EvidenceApi(authorize=lambda _: None, service_factory=lambda: service, policy={"different": True}, enabled=True)
    assert api.handle({}, request["requestId"], "submit", json_bytes(request))[0] == 422
    assert not storage.registry and not starter.calls


@pytest.mark.parametrize("body", [b'{"a":1,"a":2}', b'{"a":NaN}', b"[]", b"invalid"])
def test_strict_json(body):
    service, request, _, _ = setup()
    api = EvidenceApi(authorize=lambda _: None, service_factory=lambda: service, policy=request["policy"], enabled=True)
    assert api.handle({}, request["requestId"], "submit", body)[0] == 422


def test_worker_replay_no_model_factory_and_policy_fail_before_model():
    service, request, _, _ = setup(); service.submit(request)
    def forbidden():
        pytest.fail("model factory accessed")
    with pytest.raises(PackageEvidenceError, match="POLICY_BINDING"):
        run_worker(service, request["requestId"], {}, forbidden)
    model = FakeModel(); model.model_name = request["policy"]["model"]
    run_worker(service, request["requestId"], request["policy"], lambda: model)
    assert run_worker(service, request["requestId"], request["policy"], forbidden)["status"] == "completed"


def test_wrong_actual_model_rejected():
    service, request, _, _ = setup(); service.submit(request)
    model = FakeModel(); model.model_name = "wrong"
    with pytest.raises(PackageEvidenceError, match="MODEL_BINDING"):
        run_worker(service, request["requestId"], request["policy"], lambda: model)
    assert model.packet is None


def test_total_read_deadline_rejects_trickling_body():
    now = [0.0]
    class Connection:
        def settimeout(self, value):
            assert value <= 10
    class Stream:
        def read1(self, size):
            now[0] += 4
            return b"x"
    with pytest.raises(TimeoutError):
        read_evidence_body(Stream(), Connection(), 10, clock=lambda: now[0])
    assert now[0] == 12


def test_shared_policy_available_in_packaged_module(monkeypatch):
    from pathlib import Path
    monkeypatch.setenv("PACKAGE_EVIDENCE_MODEL_DEPLOYMENT", "explicit-test")
    assert configured_policy()["model"] == "explicit-test"
    docker = Path("Dockerfile.deterministic").read_text()
    assert "COPY bom_processor" in docker
    source = Path("deterministic_api.py").read_text()
    assert "from evidence_job import" not in source


def test_worker_failure_log_names_stable_cause_chain_without_content(monkeypatch, capsys):
    # Regression: A026 (2026-10-07) evidence job failed and the only log line was
    # EVIDENCE_WORKER_FAILED, so the failing step could not be identified.
    import json
    import sys
    from types import SimpleNamespace
    import evidence_job
    import bom_processor.package_evidence_foundry as foundry
    service, request, _, _ = setup(); service.submit(request)
    secret = "CUSTOMER-PART-XYZ provider body"

    class Broken(FakeModel):
        def propose(self, packet):
            raise KeyError(secret)
    model = Broken(); model.model_name = request["policy"]["model"]
    monkeypatch.setenv("PACKAGE_EVIDENCE_MODEL_ENABLED", "true")
    monkeypatch.setenv("REQUEST_ID", request["requestId"])
    monkeypatch.setitem(sys.modules, "deterministic_api", SimpleNamespace(_storage_factory=lambda: service.storage))
    monkeypatch.setattr(foundry, "create_foundry_evidence_model", lambda: model)
    monkeypatch.setattr(evidence_job, "configured_policy", lambda: request["policy"])
    assert evidence_job.main() == 1
    out = capsys.readouterr().out
    assert json.loads(out) == {"status": "error", "code": "EVIDENCE_WORKER_FAILED",
                               "causes": ["EVIDENCE_JOB_FAILED", "MODEL_PROPOSAL_FAILED", "KeyError"]}
    assert "CUSTOMER" not in out and "provider" not in out


def test_failure_causes_honour_from_none_and_reject_unstructured_messages():
    from evidence_job import failure_causes
    try:
        try:
            raise RuntimeError("endpoint https://secret token")
        except RuntimeError:
            raise PackageEvidenceError("MODEL_REQUEST_FAILED") from None
    except PackageEvidenceError as exc:
        assert failure_causes(exc) == ["MODEL_REQUEST_FAILED"]
    assert failure_causes(PackageEvidenceError("free text with data")) == ["UNSTRUCTURED_EVIDENCE_ERROR"]
    assert failure_causes(ValueError("x"), limit=1) == ["ValueError"]