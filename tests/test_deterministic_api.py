import base64
import json
import threading
from email.message import Message

import pytest

from bom_processor import BlobConflictError, BlobNotFoundError, ProcessingError
from deterministic_api import ApiConfig, ProcessingApi, authorize


class StubProcessor:
    def __init__(self, result=None, error=None):
        self.result = result or {"status": "completed"}
        self.error = error
        self.requests = []

    def process(self, request):
        self.requests.append(request)
        if self.error:
            raise self.error
        return self.result


def config(*, auth_mode="local_disabled", max_body=1024, concurrency=1):
    return ApiConfig(
        host="127.0.0.1", port=8088, auth_mode=auth_mode,
        allowed_app_ids=frozenset({"approved-app"}),
        allowed_object_ids=frozenset({"approved-object"}),
        max_body_bytes=max_body, max_concurrent_jobs=concurrency,
        processor_options={},
    )


def headers(**values):
    message = Message()
    for key, value in values.items():
        message[key.replace("_", "-")] = value
    return message


def principal(**claims):
    value = {"claims": [{"typ": name, "val": claim} for name, claim in claims.items()]}
    return base64.b64encode(json.dumps(value).encode()).decode()


def body():
    return json.dumps({
        "job_id": "job1", "customer_key": "cust", "parser_key": "Astronics",
        "rfq_uuid": "12345678-1234-4234-8234-123456789012",
        "package_version": "v1", "documents": [{
            "document_id": "doc1",
            "blob_path": "cust/rfq-12345678-1234-4234-8234-123456789012/package-v1/original-boms/doc1-input.xlsx",
            "size_bytes": 1, "sha256": "0" * 64,
        }],
    }).encode()


def test_easy_auth_requires_identity_and_explicit_allowlist():
    api = ProcessingApi(config(auth_mode="easy_auth"), lambda: StubProcessor())
    assert api.handle(headers(), body())[0] == 401
    denied = headers(X_MS_CLIENT_PRINCIPAL=principal(appid="other"))
    assert api.handle(denied, body())[0] == 403


@pytest.mark.parametrize("claim,value", [("appid", "approved-app"), ("oid", "approved-object")])
def test_easy_auth_accepts_allowlisted_app_or_object(claim, value):
    identity = headers(X_MS_CLIENT_PRINCIPAL=principal(**{claim: value}))
    authorize(identity, config(auth_mode="easy_auth"))
    assert ProcessingApi(config(auth_mode="easy_auth"), lambda: StubProcessor()).handle(identity, body())[0] == 200


def test_malformed_identity_json_and_claims_are_unauthenticated():
    api = ProcessingApi(config(auth_mode="easy_auth"), lambda: StubProcessor())
    for encoded in ("not-base64", base64.b64encode(b"{}").decode()):
        assert api.handle(headers(X_MS_CLIENT_PRINCIPAL=encoded), body())[0] == 401


def test_body_and_json_validation_prevent_processor_call():
    processor = StubProcessor()
    api = ProcessingApi(config(max_body=10), lambda: processor)
    assert api.handle(headers(), b"x" * 11)[0] == 413
    assert api.handle(headers(), b"{")[0] == 400
    assert processor.requests == []


def test_invalid_processing_request_returns_safe_422():
    status, payload = ProcessingApi(config(), lambda: StubProcessor()).handle(headers(), b"{}")
    assert status == 422
    assert payload["error"]["code"] == "invalid_request"


def test_candidate_required_response_includes_contract_version():
    from bom_processor import CandidateRequiredError

    error = CandidateRequiredError("request-1", "Parser1", "a" * 64)
    api = ProcessingApi(config(), lambda: StubProcessor(error=error))
    status, payload = api.handle(headers(), body())
    assert status == 202
    assert payload == {"version": "1", "status": "candidate_required", "candidate": {
        "request_id": "request-1", "parser_key": "Parser1", "profile_hash": "a" * 64}}


def test_force_candidate_is_strictly_validated_and_bound_to_request():
    from bom_processor import ProcessingRequest

    value = json.loads(body())
    value["force_candidate"] = True
    request = ProcessingRequest.from_dict(value)
    assert request.force_candidate is True
    assert request.fingerprint() != ProcessingRequest.from_dict({
        key: item for key, item in value.items() if key != "force_candidate"
    }).fingerprint()
    value["force_candidate"] = "true"
    with pytest.raises(ProcessingError, match="invalid request"):
        ProcessingRequest.from_dict(value)


@pytest.mark.parametrize("error,status,code", [
    (BlobConflictError("private"), 409, "immutable_conflict"),
    (BlobNotFoundError("private"), 404, "artifact_not_found"),
    (ProcessingError("parser timed out"), 504, "parser_timeout"),
    (ProcessingError("local secret path"), 422, "processing_failed"),
    (RuntimeError("storage connection string"), 500, "internal_error"),
])
def test_processing_errors_map_to_safe_responses(error, status, code):
    result_status, payload = ProcessingApi(config(), lambda: StubProcessor(error=error)).handle(headers(), body())
    assert result_status == status
    assert payload["error"]["code"] == code
    assert str(error) not in json.dumps(payload)


def test_concurrency_limit_returns_429():
    entered = threading.Event()
    release = threading.Event()

    class BlockingProcessor(StubProcessor):
        def process(self, request):
            entered.set()
            release.wait(timeout=2)
            return self.result

    api = ProcessingApi(config(concurrency=1), lambda: BlockingProcessor())
    thread = threading.Thread(target=lambda: api.handle(headers(), body()))
    thread.start()
    assert entered.wait(timeout=1)
    status, payload = api.handle(headers(), body())
    release.set()
    thread.join(timeout=2)
    assert status == 429
    assert payload["error"]["code"] == "busy"


def test_env_defaults_fail_closed_without_allowlist(monkeypatch):
    monkeypatch.setenv("BOM_API_AUTH_MODE", "easy_auth")
    monkeypatch.delenv("BOM_API_ALLOWED_APP_IDS", raising=False)
    monkeypatch.delenv("BOM_API_ALLOWED_OBJECT_IDS", raising=False)
    from deterministic_api import ApiConfig
    with pytest.raises(ValueError, match="allowlist"):
        ApiConfig.from_env()
