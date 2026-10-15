import json
import sys
from types import ModuleType

import validation_job


def final_line(out):
    """The existing result line stays last; the job identity START/END lines come before it (QQ #121)."""
    lines = out.strip().splitlines()
    assert [json.loads(line)["phase"] for line in lines[:-1]] == ["start", "end"]
    return json.loads(lines[-1])


def install_fakes(monkeypatch, service):
    azure = ModuleType("azure")
    identity = ModuleType("azure.identity")
    identity.ManagedIdentityCredential = lambda **_: object()
    azure.identity = identity
    monkeypatch.setitem(sys.modules, "azure", azure)
    monkeypatch.setitem(sys.modules, "azure.identity", identity)
    monkeypatch.setattr("bom_processor.core.AzureBlobStorage.from_account_url",
                        lambda *args, **kwargs: object())
    monkeypatch.setattr("bom_processor.job_orchestrator.AzureSasFactory",
                        lambda *args, **kwargs: object())
    monkeypatch.setattr("bom_processor.job_orchestrator.AzureJobStarter",
                        lambda *args, **kwargs: object())
    monkeypatch.setattr("bom_processor.job_orchestrator.JobOrchestrator",
                        lambda *args, **kwargs: object())
    monkeypatch.setattr("bom_processor.candidate_validation.CandidateValidationService", service)


def configure(monkeypatch):
    values = {
        "REQUEST_ID": "request-1", "CANDIDATE_ID": "candidate-1",
        "BOM_STORAGE_ACCOUNT_URL": "https://example.blob.core.usgovcloudapi.net",
        "BOM_JOB_SUBSCRIPTION_ID": "subscription", "BOM_JOB_RESOURCE_GROUP": "rg",
        "BOM_JOB_NAME": "worker", "BOM_WORKER_IMAGE": "registry/worker:version",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)


def test_unknown_request_environment_is_rejected(monkeypatch, capsys):
    configure(monkeypatch)
    monkeypatch.setenv("CANDIDATE_SECRET", "not-accepted")
    assert validation_job.main() == 2
    assert final_line(capsys.readouterr().out) == {
        "error": "invalid_request_environment", "status": "error"}


def test_success_prints_only_safe_identifiers(monkeypatch, capsys):
    class Result:
        status = "awaiting_approval"
        request_id = "request-1"
        candidate_id = "candidate-1"
        validation_id = "validation-1"
        evidence_path = "candidates/request-1/candidate-1/validation/validation-1/evidence.json"

    class Service:
        def __init__(self, *args, **kwargs):
            pass

        def validate(self):
            return Result()

    configure(monkeypatch)
    install_fakes(monkeypatch, Service)
    assert validation_job.main() == 0
    result = final_line(capsys.readouterr().out)
    assert result["status"] == "awaiting_approval"
    assert result["validation_id"] == "validation-1"
    assert "source" not in result and "code" not in result and "url" not in result


def test_safe_validation_error_is_reported(monkeypatch, capsys):
    from bom_processor.candidate_validation import CandidateValidationError

    class Service:
        def __init__(self, *args, **kwargs):
            pass

        def validate(self):
            raise CandidateValidationError("SOURCE_INTEGRITY")

    configure(monkeypatch)
    install_fakes(monkeypatch, Service)
    assert validation_job.main() == 1
    assert final_line(capsys.readouterr().out)["error"] == "SOURCE_INTEGRITY"