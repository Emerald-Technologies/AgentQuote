"""Offline doubles only: no credentials, samples or Government service calls."""
import asyncio
import json
import sys
from types import SimpleNamespace

import pytest

from bom_processor.package_evidence_contracts import PackageEvidenceError, digest
from bom_processor.package_evidence_foundry import (
    FoundryEvidenceProposalModel, SYSTEM_INSTRUCTION, PROMPT_SHA256,
    MAX_PACKET_BYTES, MAX_RESPONSE_BYTES, parse_response, create_foundry_evidence_model,
    MAX_MODEL_INPUT_BYTES, MAX_OUTPUT_TOKENS, MODEL_DEADLINE_SECONDS,
)
from test_package_evidence_proposal import inputs, FakeModel
from test_package_part_classification import ClassifiedModel
from test_package_manufacturing_assessment import AssessedModel, assessed_inputs


def test_tool_free_packet_and_exact_prompt_provenance():
    seen = {}

    class Agent:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        async def run(self, packet, *, options):
            seen["options"] = options
            seen["packet"] = json.loads(packet)
            from test_package_evidence_retention import RetentionModel
            return SimpleNamespace(output_text=json.dumps(RetentionModel().propose(seen["packet"])))

    model = FoundryEvidenceProposalModel(object(), model_name="explicit-deployment", agent_factory=Agent)
    context, roots, books = assessed_inputs()
    from test_package_evidence_inventory import root
    from test_package_evidence_retention import source_proof
    roots.append(root(b"original BOM source bytes", "original.xlsx", "12345678-1234-4234-8234-123456789080"))
    result = model.produce(context, roots, books, run_id="test-run", retained_bom_sources=source_proof(roots))
    graph = result["proposal"]
    assert len(result["classification"]["classifications"]) == len(graph["occurrences"])
    assert result["manufacturingAssessment"]["proposalSha256"] == graph["proposalSha256"]
    assert seen["tools"] == []
    assert seen["options"] == {"max_tokens": MAX_OUTPUT_TOKENS, "store": False,
                               "response_format": {"type": "json_object"}}
    assert seen["instructions"] == SYSTEM_INSTRUCTION
    assert graph["provenance"]["promptSha256"] == PROMPT_SHA256 == digest(SYSTEM_INSTRUCTION.encode())
    assert graph["provenance"]["model"] == "explicit-deployment"
    assert "rowObservations" in seen["packet"]
    assert any(link["scope"] == "shared" for link in graph["associations"])


@pytest.mark.parametrize("value", [None, [], "[]", "null", "```json\n{}\n```", "{} trailing",
    '{"associations":[],"dispositions":[],"extra":1}',
    '{"associations":[],"dispositions":[],"associations":[]}',
    '{"associations":[],"dispositions":[{"a":1,"a":2}]}',
    '{"associations":[],"dispositions":NaN}',
    '{"associations":{},"dispositions":[]}'])
def test_strict_json_rejection(value):
    with pytest.raises(PackageEvidenceError):
        parse_response(value)


def _shape_causes(value):
    from evidence_job import failure_causes
    with pytest.raises(PackageEvidenceError) as caught:
        parse_response(value)
    assert str(caught.value) == "MODEL_RESPONSE_INVALID"
    return failure_causes(caught.value)


VALID = {"associations": [], "dispositions": [], "classifications": [], "manufacturing": {}}


def test_valid_shapes_still_parse():
    assert parse_response(json.dumps(VALID)) == VALID
    assert parse_response(json.dumps({**VALID, "categories": []}))["categories"] == []


@pytest.mark.parametrize("value, expected", [
    ([], ["MODEL_SHAPE_NOT_OBJECT_ARRAY"]),
    ({"associations": [], "dispositions": []},
     ["MODEL_SHAPE_MISSING_CLASSIFICATIONS", "MODEL_SHAPE_MISSING_MANUFACTURING"]),
    ({**VALID, "manufacturing": []}, ["MODEL_SHAPE_TYPE_MANUFACTURING_ARRAY"]),
    ({**VALID, "associations": {}, "categories": None},
     ["MODEL_SHAPE_TYPE_ASSOCIATIONS_OBJECT", "MODEL_SHAPE_TYPE_CATEGORIES_NULL"]),
    ({**VALID, "Requirements": [], "relationships": []},
     ["MODEL_SHAPE_EXTRA_RELATIONSHIPS", "MODEL_SHAPE_EXTRA_REQUIREMENTS"]),
    ({"proposal": VALID}, ["MODEL_SHAPE_WRAPPED_PROPOSAL", "MODEL_SHAPE_INNER_VALID"]),
    ({"result": {"associations": []}}, ["MODEL_SHAPE_WRAPPED_RESULT", "MODEL_SHAPE_INNER_INVALID"]),
])
def test_shape_failure_names_the_gap(value, expected):
    assert _shape_causes(json.dumps(value)) == ["MODEL_RESPONSE_INVALID", *expected]


def test_shape_diagnostics_never_echo_model_chosen_keys():
    secret = "PN-12345 customer drawing.pdf"
    causes = _shape_causes(json.dumps({**VALID, secret: [], "TEST_ONLY_ASM_100": 1}))
    assert causes == ["MODEL_RESPONSE_INVALID", "MODEL_SHAPE_EXTRA_OTHER"]
    wrapped = _shape_causes(json.dumps({secret: VALID}))
    assert wrapped == ["MODEL_RESPONSE_INVALID", "MODEL_SHAPE_WRAPPED_OTHER", "MODEL_SHAPE_INNER_VALID"]
    assert not any("12345" in cause or "PDF" in cause for cause in causes + wrapped)


def test_shape_details_are_bounded_and_fit_the_worker_cause_limit():
    from evidence_job import failure_causes
    bad = {"associations": 1, "dispositions": "x", "classifications": None, "manufacturing": [],
           "categories": 2, "notes": []}
    causes = _shape_causes(json.dumps(bad))
    assert len(causes) == 1 + 5 and causes[-1] == "MODEL_SHAPE_MORE"
    # Full worker chain: job -> proposal -> response -> at most 5 shape codes = 8 (the log limit).
    Err = PackageEvidenceError
    with pytest.raises(Err) as caught:
        parse_response(json.dumps(bad))
    try:
        try:
            raise Err("MODEL_PROPOSAL_FAILED") from caught.value
        except Err as proposal:
            raise Err("EVIDENCE_JOB_FAILED") from proposal
    except Err as job:
        chain = failure_causes(job)
    assert chain[:3] == ["EVIDENCE_JOB_FAILED", "MODEL_PROPOSAL_FAILED", "MODEL_RESPONSE_INVALID"]
    assert chain[-1] == "MODEL_SHAPE_MORE" and len(chain) == 8


def test_propose_preserves_shape_causes_through_agent_call():
    from evidence_job import failure_causes

    class Agent:
        def __init__(self, **kwargs):
            pass

        async def run(self, packet, *, options):
            return SimpleNamespace(output_text=json.dumps({"proposal": VALID}))

    model = FoundryEvidenceProposalModel(object(), model_name="test", agent_factory=Agent)
    with pytest.raises(PackageEvidenceError) as caught:
        model.propose({"small": "packet"})
    assert failure_causes(caught.value) == [
        "MODEL_RESPONSE_INVALID", "MODEL_SHAPE_WRAPPED_PROPOSAL", "MODEL_SHAPE_INNER_VALID"]


def test_propose_records_raw_reply_even_when_parsing_fails():
    replies = iter(['{"proposal": {}}', "not json"])

    class Agent:
        def __init__(self, **kwargs):
            pass

        async def run(self, packet, *, options):
            return SimpleNamespace(output_text=next(replies))

    model = FoundryEvidenceProposalModel(object(), model_name="test", agent_factory=Agent)
    assert model.last_producer_reply is None
    for expected, code in (('{"proposal": {}}', "MODEL_RESPONSE_INVALID"), ("not json", "MODEL_JSON_INVALID")):
        with pytest.raises(PackageEvidenceError, match=code):
            model.propose({"small": "packet"})
        assert model.last_producer_reply == expected

    class Failing:
        def __init__(self, **kwargs):
            pass

        async def run(self, packet, *, options):
            raise RuntimeError("provider body")

    model.agent_factory = Failing
    with pytest.raises(PackageEvidenceError, match="MODEL_REQUEST_FAILED"):
        model.propose({"small": "packet"})
    assert model.last_producer_reply is None


def test_response_and_input_limits_before_invocation():
    with pytest.raises(PackageEvidenceError, match="LIMIT"):
        parse_response(" " * (MAX_RESPONSE_BYTES + 1))
    calls = []
    model = FoundryEvidenceProposalModel(object(), model_name="test", agent_factory=lambda **kwargs: calls.append(kwargs))
    with pytest.raises(PackageEvidenceError, match="PACKET_LIMIT"):
        model.propose({"text": "x" * MAX_PACKET_BYTES})
    assert calls == []


def test_combined_instructions_and_packet_bytes_fail_before_agent_creation():
    calls = []
    model = FoundryEvidenceProposalModel(object(), model_name="test", agent_factory=lambda **kwargs: calls.append(kwargs))
    with pytest.raises(PackageEvidenceError, match="MODEL_INPUT_BYTE_LIMIT"):
        model.propose({"text": "x" * MAX_MODEL_INPUT_BYTES})
    assert calls == []


def test_timeout_and_provider_failure_do_not_retry_or_expose_details():
    calls = []

    class Agent:
        def __init__(self, **kwargs):
            pass

        async def run(self, text, *, options):
            calls.append(text)
            raise RuntimeError("private-provider-body")

    with pytest.raises(PackageEvidenceError, match="MODEL_REQUEST_FAILED") as error:
        FoundryEvidenceProposalModel(object(), model_name="test", agent_factory=Agent).propose({})
    assert "private" not in str(error.value)
    assert len(calls) == 1

    class TimedAgent(Agent):
        async def run(self, text, *, options):
            raise asyncio.TimeoutError()

    with pytest.raises(PackageEvidenceError, match="MODEL_TIMEOUT"):
        FoundryEvidenceProposalModel(object(), model_name="test", agent_factory=TimedAgent).propose({})


@pytest.mark.parametrize("seconds", [True, 0, 121, 301, 1.5])
def test_invalid_deadline(seconds):
    with pytest.raises(PackageEvidenceError):
        FoundryEvidenceProposalModel(object(), model_name="test", timeout_seconds=seconds)


def test_disabled_before_importing_clients(monkeypatch):
    monkeypatch.delenv("PACKAGE_EVIDENCE_MODEL_ENABLED", raising=False)
    with pytest.raises(PackageEvidenceError, match="MODEL_DISABLED"):
        create_foundry_evidence_model()


@pytest.mark.parametrize("endpoint", ["", "http://example.azure.us/p", "https://example.azure.com/p",
    "https://example.azure.us.evil.test/p", "https://user:pass@example.azure.us/p",
    "https://example.azure.us/p?token=x", "https://example.azure.us:444/p", "https://example.azure.us/p#x"])
def test_unsafe_endpoint_rejected_before_client_import(monkeypatch, endpoint):
    monkeypatch.setenv("PACKAGE_EVIDENCE_MODEL_ENABLED", "true")
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", endpoint)
    with pytest.raises(PackageEvidenceError, match="ENDPOINT_INVALID"):
        create_foundry_evidence_model()


def test_no_default_deployment(monkeypatch):
    monkeypatch.setenv("PACKAGE_EVIDENCE_MODEL_ENABLED", "true")
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", "https://synthetic.azure.us/project")
    monkeypatch.delenv("PACKAGE_EVIDENCE_MODEL_DEPLOYMENT", raising=False)
    with pytest.raises(PackageEvidenceError, match="INVALID_TEXT"):
        create_foundry_evidence_model()


def test_factory_uses_explicit_deployment_managed_identity_and_government_scope(monkeypatch):
    seen = {}

    class Credential:
        def __init__(self, **kwargs):
            seen["credential"] = kwargs

        def get_token(self, *scopes, **kwargs):
            seen["scopes"] = scopes
            return object()

    def client(**kwargs):
        seen["client"] = kwargs
        value = SimpleNamespace(client=SimpleNamespace(max_retries=2, timeout=600.0))
        seen["inner"] = value.client
        return value

    monkeypatch.setitem(sys.modules, "agent_framework_foundry", SimpleNamespace(FoundryChatClient=client))
    monkeypatch.setitem(sys.modules, "azure.identity", SimpleNamespace(ManagedIdentityCredential=Credential))
    monkeypatch.setenv("PACKAGE_EVIDENCE_MODEL_ENABLED", "true")
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", "https://synthetic.azure.us/project")
    monkeypatch.setenv("PACKAGE_EVIDENCE_MODEL_DEPLOYMENT", "explicit-test-model")
    monkeypatch.setenv("AZURE_CLIENT_ID", "synthetic-client-id")
    result = create_foundry_evidence_model()
    assert result.model_name == "explicit-test-model"
    assert seen["credential"] == {"client_id": "synthetic-client-id"}
    assert seen["client"]["model"] == "explicit-test-model"
    seen["client"]["credential"].get_token("https://ai.azure.com/.default")
    assert seen["scopes"] == ("https://ai.azure.us/.default",)
    assert seen["inner"].max_retries == 0
    assert seen["inner"].timeout == float(MODEL_DEADLINE_SECONDS)