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
)
from test_package_evidence_proposal import inputs, FakeModel
from test_package_part_classification import ClassifiedModel


def test_tool_free_packet_and_exact_prompt_provenance():
    seen = {}

    class Agent:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        async def run(self, packet):
            seen["packet"] = json.loads(packet)
            return SimpleNamespace(output_text=json.dumps(ClassifiedModel().propose(seen["packet"])))

    model = FoundryEvidenceProposalModel(object(), model_name="explicit-deployment", agent_factory=Agent)
    context, roots, books = inputs()
    result = model.produce(context, roots, books, run_id="test-run")
    graph = result["proposal"]
    assert len(result["classification"]["classifications"]) == len(graph["occurrences"])
    assert seen["tools"] == []
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


def test_response_and_input_limits_before_invocation():
    with pytest.raises(PackageEvidenceError, match="LIMIT"):
        parse_response(" " * (MAX_RESPONSE_BYTES + 1))
    calls = []
    model = FoundryEvidenceProposalModel(object(), model_name="test", agent_factory=lambda **kwargs: calls.append(kwargs))
    with pytest.raises(PackageEvidenceError, match="PACKET_LIMIT"):
        model.propose({"text": "x" * MAX_PACKET_BYTES})
    assert calls == []


def test_timeout_and_provider_failure_do_not_retry_or_expose_details():
    calls = []

    class Agent:
        def __init__(self, **kwargs):
            pass

        async def run(self, text):
            calls.append(text)
            raise RuntimeError("private-provider-body")

    with pytest.raises(PackageEvidenceError, match="MODEL_REQUEST_FAILED") as error:
        FoundryEvidenceProposalModel(object(), model_name="test", agent_factory=Agent).propose({})
    assert "private" not in str(error.value)
    assert len(calls) == 1

    class TimedAgent(Agent):
        async def run(self, text):
            raise asyncio.TimeoutError()

    with pytest.raises(PackageEvidenceError, match="MODEL_TIMEOUT"):
        FoundryEvidenceProposalModel(object(), model_name="test", agent_factory=TimedAgent).propose({})


@pytest.mark.parametrize("seconds", [True, 0, 301, 1.5])
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
        return object()

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