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
    expected = {"failure_stage": "response", "failure_category": category}
    if category == "json_invalid":
        expected["failure_json_reason"] = "other"  # message is not a known stdlib phrase
    assert result.value.diagnostics == expected
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
    assert failed.value.diagnostics == {"failure_stage": "response", "failure_category": "json_invalid",
                                        "failure_json_reason": "expecting_value"}


@pytest.mark.parametrize("reply,reason", [
    # Shapes that a complete (untruncated) bundle reply can take; A025-A027 failures were one of these.
    ('```json\n{"parser_code": "x"}\n```', "expecting_value"),
    ('{"parser_code": "import re\nprint(1)"}', "invalid_control_character"),
    ('{"parser_code": "re.compile(\'\\d+\')"}', "invalid_escape"),
    ('{"parser_code": "x"}\nDone.', "extra_data"),
    ('{"parser_code": "x', "unterminated_string"),
    ('{"parser_code": "x" "y": 1}', "expecting_delimiter"),
])
def test_json_reason_names_malformation_kind_without_content(monkeypatch, reply, reason):
    prompt = SimpleNamespace(canonical_columns=(), rules=())
    sources = [{"bytes": b"synthetic", "extension": "xlsx", "sha256": "a" * 64}]
    with pytest.raises(AuthorFailure) as failed:
        bundle_model(monkeypatch, response=reply + SECRET if reason == "extra_data" else reply).generate_bundle(prompt, sources)
    assert failed.value.diagnostics["failure_json_reason"] == reason
    assert SECRET not in repr(failed.value.diagnostics) and "parser_code" not in repr(failed.value.diagnostics)


def test_json_reason_is_allowlisted_and_only_for_json_failures():
    assert diagnostic_fields("response", "json_invalid", json_reason="expecting_value")["failure_json_reason"] == "expecting_value"
    assert "failure_json_reason" not in diagnostic_fields("response", "json_invalid", json_reason=SECRET)
    assert "failure_json_reason" not in diagnostic_fields("response", "timeout", json_reason="extra_data")
    weird = json.JSONDecodeError("Some future message " + SECRET, "x", 0)
    assert AuthorFailure("response", weird).diagnostics["failure_json_reason"] == "other"


def test_bundle_call_requests_service_enforced_json(monkeypatch):
    seen = {}
    monkeypatch.setattr("bom_processor.foundry_author.build_complete_inventory", lambda *_: '{"cells":[]}')
    class Agent:
        def __init__(self, **_): pass
        async def run(self, *_args, options):
            seen.update(options)
            return SimpleNamespace(output_text='{"parser_code": "x", "assembly_associations": {}}')
    FoundryModelAuthor(agent_factory=Agent).generate_bundle(SimpleNamespace(canonical_columns=(), rules=()),
        [{"bytes": b"synthetic", "extension": "xlsx", "sha256": "a" * 64}])
    assert seen == {"max_tokens": 8192, "response_format": {"type": "json_object"}}


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
    assert failed.value.diagnostics == {"failure_stage": "response", "failure_category": "json_invalid",
                                        "failure_json_reason": "other"}
    assert store.candidates == before


def test_association_failure_has_safe_stage_and_preserves_artifacts(monkeypatch):
    from test_source_assembly_association import roles, setup
    store, sources, associations, _ = setup()
    path = "candidates/requests/xlsx-demo.json"
    request = json.loads(store.registry[path])
    request["version"] = "candidate-request.v2"
    store.registry[path] = json.dumps(request).encode()
    before = dict(store.candidates)
    class Model:
        def generate_bundle(self, *_):
            return {"parser_code": "unused", "document_roles": roles(store, sources),
                    "assembly_associations": associations}
    def fail(*_): raise ValueError(SECRET)
    monkeypatch.setattr("bom_processor.assembly_association.verify_association", fail)
    with pytest.raises(AuthoringError) as failed:
        CandidateAuthoringService("xlsx-demo", store, Model(), source_associations=True).author()
    assert failed.value.code == "MODEL_FAILED"
    assert failed.value.diagnostics == {"failure_stage": "association", "failure_category": "value_error"}
    # Only the verified role record was added; no parser or association artifacts.
    added = set(store.candidates) - set(before)
    assert len(added) == 1 and next(iter(added)).startswith("candidates/xlsx-demo/document-roles/")