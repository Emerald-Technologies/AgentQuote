"""Offline failure-stage accuracy and content-leak regressions."""
import json
from types import SimpleNamespace

import pytest

from bom_processor.author_failure import AuthorFailure, diagnostic_fields, failure_stage
from bom_processor.candidate_authoring import AuthoringError, CandidateAuthoringService
from bom_processor.foundry_author import FoundryModelAuthor

SECRET = "https://private.example/token secret-response user@example.test"


@pytest.mark.parametrize("exception,category", [
    (ValueError(SECRET), "value_error"), (TypeError(SECRET), "type_error"),
    (TimeoutError(SECRET), "timeout"), (ConnectionError(SECRET), "connection"),
    (json.JSONDecodeError(SECRET, SECRET, 0), "json_invalid"),
    (RuntimeError(SECRET), "unexpected"),
])
def test_classification_never_records_exception_content(exception, category):
    with pytest.raises(AuthorFailure) as result:
        with failure_stage("response"):
            raise exception
    assert result.value.diagnostics == {"failure_stage": "response", "failure_category": category}
    assert SECRET not in str(result.value)
    assert SECRET not in repr(result.value.diagnostics)


def test_http_status_is_numeric_allowlisted_only():
    error_type = type("RateLimitError", (Exception,), {})
    error = error_type(SECRET)
    error.status_code = 429
    assert AuthorFailure("invocation", error).diagnostics["failure_http_status"] == 429
    for status in (True, "429", 999, SECRET):
        error.status_code = status
        assert "failure_http_status" not in AuthorFailure("invocation", error).diagnostics
    assert diagnostic_fields(SECRET, "unexpected") == {}
    assert diagnostic_fields("response", SECRET) == {}


def bundle_model(monkeypatch, response=None, exception=None):
    monkeypatch.setattr("bom_processor.foundry_author.build_complete_inventory", lambda *_: '{"cells":[]}')
    class Agent:
        def __init__(self, **_): pass
        async def run(self, *_args, **_kwargs):
            if exception:
                raise exception
            return SimpleNamespace(output_text=response)
    return FoundryModelAuthor(agent_factory=Agent)


def test_invocation_failure_is_distinct_from_response_json(monkeypatch):
    prompt = SimpleNamespace(canonical_columns=(), rules=())
    sources = [{"bytes": b"synthetic", "extension": "xlsx", "sha256": "a" * 64}]
    with pytest.raises(AuthorFailure) as failed:
        bundle_model(monkeypatch, exception=TimeoutError(SECRET)).generate_bundle(prompt, sources)
    assert failed.value.diagnostics == {"failure_stage": "invocation", "failure_category": "timeout"}
    with pytest.raises(AuthorFailure) as failed:
        bundle_model(monkeypatch, response=SECRET).generate_bundle(prompt, sources)
    assert failed.value.diagnostics == {"failure_stage": "response", "failure_category": "json_invalid"}


def test_inventory_failure_occurs_before_model_factory(monkeypatch):
    calls = []
    def fail(*_): raise ValueError(SECRET)
    monkeypatch.setattr("bom_processor.foundry_author.build_complete_inventory", fail)
    with pytest.raises(AuthorFailure) as failed:
        FoundryModelAuthor(agent_factory=lambda **_: calls.append(True)).generate_bundle(
            SimpleNamespace(canonical_columns=(), rules=()),
            [{"bytes": b"synthetic", "extension": "xlsx", "sha256": "a" * 64}])
    assert failed.value.diagnostics["failure_stage"] == "inventory"
    assert calls == []


def test_safe_diagnostics_survive_service_boundary(monkeypatch):
    from test_source_assembly_association import setup
    store, _, _, _ = setup()
    path = "candidates/requests/xlsx-demo.json"
    request = json.loads(store.registry[path])
    request["version"] = "candidate-request.v2"
    store.registry[path] = json.dumps(request).encode()
    before = dict(store.candidates)
    class Model:
        def generate_bundle(self, *_): raise AuthorFailure("response", json.JSONDecodeError(SECRET, SECRET, 0))
    with pytest.raises(AuthoringError) as failed:
        CandidateAuthoringService("xlsx-demo", store, Model(), source_associations=True).author()
    assert failed.value.code == "MODEL_FAILED"
    assert failed.value.diagnostics == {"failure_stage": "response", "failure_category": "json_invalid"}
    assert store.candidates == before


def test_association_failure_has_safe_stage_and_preserves_artifacts(monkeypatch):
    from test_source_assembly_association import setup
    store, _, associations, _ = setup()
    path = "candidates/requests/xlsx-demo.json"
    request = json.loads(store.registry[path])
    request["version"] = "candidate-request.v2"
    store.registry[path] = json.dumps(request).encode()
    before = dict(store.candidates)
    class Model:
        def generate_bundle(self, *_):
            return {"parser_code": "unused", "assembly_associations": associations}
    def fail(*_): raise ValueError(SECRET)
    monkeypatch.setattr("bom_processor.assembly_association.verify_association", fail)
    with pytest.raises(AuthoringError) as failed:
        CandidateAuthoringService("xlsx-demo", store, Model(), source_associations=True).author()
    assert failed.value.code == "MODEL_FAILED"
    assert failed.value.diagnostics == {"failure_stage": "association", "failure_category": "value_error"}
    assert store.candidates == before