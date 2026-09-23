import json

import candidate_job


def test_unknown_request_environment_is_rejected(monkeypatch, capsys):
    monkeypatch.setenv("REQUEST_ID", "request-1")
    monkeypatch.setenv("REQUEST_SECRET", "must-not-be-accepted")
    assert candidate_job.main() == 2
    result = json.loads(capsys.readouterr().out)
    assert result == {"error": "invalid_request_environment", "status": "error"}


def test_job_success_prints_safe_identifiers(monkeypatch, capsys):
    class Result:
        status = "generated_unvalidated"
        request_id = "request-1"
        candidate_id = "candidate-1"
        manifest_path = "candidates/request-1/candidate-1/candidate-manifest.json"

    class Service:
        def __init__(self, *args):
            pass
        def author(self):
            return Result()

    monkeypatch.setenv("REQUEST_ID", "request-1")
    monkeypatch.setenv("BOM_STORAGE_ACCOUNT_URL", "https://example.blob.core.usgovcloudapi.net")
    import sys
    from types import ModuleType
    azure = ModuleType("azure")
    identity = ModuleType("azure.identity")
    identity.ManagedIdentityCredential = lambda **_: object()
    azure.identity = identity
    monkeypatch.setitem(sys.modules, "azure", azure)
    monkeypatch.setitem(sys.modules, "azure.identity", identity)
    monkeypatch.setattr("bom_processor.candidate_authoring.CandidateAuthoringService", Service)
    monkeypatch.setattr("bom_processor.foundry_author.create_foundry_author", lambda: object())
    monkeypatch.setattr("bom_processor.core.AzureBlobStorage.from_account_url", lambda *args, **kwargs: object())
    assert candidate_job.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["candidate_id"] == "candidate-1"
    assert "source" not in result and "code" not in result


def test_job_reports_safe_authoring_error_code(monkeypatch, capsys):
    from bom_processor.candidate_authoring import AuthoringError

    class Service:
        def __init__(self, *args): pass
        def author(self): raise AuthoringError("CANDIDATE_CODE_POLICY")

    monkeypatch.setenv("REQUEST_ID", "request-1")
    monkeypatch.setenv("BOM_STORAGE_ACCOUNT_URL", "https://example.blob.core.usgovcloudapi.net")
    import sys
    from types import ModuleType
    azure = ModuleType("azure"); identity = ModuleType("azure.identity")
    identity.ManagedIdentityCredential = lambda **_: object(); azure.identity = identity
    monkeypatch.setitem(sys.modules, "azure", azure); monkeypatch.setitem(sys.modules, "azure.identity", identity)
    monkeypatch.setattr("bom_processor.candidate_authoring.CandidateAuthoringService", Service)
    monkeypatch.setattr("bom_processor.foundry_author.create_foundry_author", lambda: object())
    monkeypatch.setattr("bom_processor.core.AzureBlobStorage.from_account_url", lambda *args, **kwargs: object())
    assert candidate_job.main() == 1
    assert json.loads(capsys.readouterr().out)["error"] == "candidate_authoring_failed"