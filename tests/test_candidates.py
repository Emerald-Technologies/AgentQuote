import json
from datetime import datetime, timezone
from email.message import Message

from bom_processor import InMemoryStorage
from bom_processor.candidates import CANDIDATE_VERSION, CandidateRequiredError, persist_candidate, request_path
from bom_processor.master_registry import profile_bytes
from deterministic_api import ApiConfig, ProcessingApi


def test_candidate_request_id_is_deterministic_and_replay_safe():
    storage = InMemoryStorage()
    profile = profile_bytes(b"part,description,qty\nP1,Widget,2\n", ".csv")
    evidence = ({"document_id": "doc1", "blob_path": "customer/input.csv", "size_bytes": 33, "sha256": "a" * 64},)
    first = persist_candidate(storage, "Customer1", "customer-key", profile, evidence,
                              lambda: datetime(2026, 9, 20, tzinfo=timezone.utc))
    second = persist_candidate(storage, "Customer1", "customer-key", profile, evidence,
                               lambda: datetime(2026, 9, 21, tzinfo=timezone.utc))
    assert second == first
    raw = json.loads(storage.registry[request_path(first.request_id)])
    assert raw["version"] == CANDIDATE_VERSION
    assert raw["status"] == "requested"
    assert raw["profile_hash"] == profile.profile_hash
    assert "part" in json.dumps(raw["profile"])


def test_equivalent_format_reuses_request_despite_different_source_evidence():
    storage = InMemoryStorage()
    profile = profile_bytes(b"part,qty\nP1,2\n", ".csv")
    first = persist_candidate(storage, "Customer1", "customer-key", profile,
                              ({"document_id": "one"},))
    second = persist_candidate(storage, "Customer1", "customer-key", profile,
                               ({"document_id": "two"},))
    assert first.request_id == second.request_id
    assert second.source_evidence == first.source_evidence
    assert len(storage.registry) == 1


def test_candidate_required_api_response_is_bounded_and_safe():
    candidate = CandidateRequiredError("f" * 64, "Customer1", "e" * 64)

    class Processor:
        def process(self, request):
            raise candidate

    config = ApiConfig("127.0.0.1", 0, "local_disabled", frozenset(), frozenset(), 4096, 1, {})
    api = ProcessingApi(config, Processor)
    body = json.dumps({
        "job_id": "job1", "customer_key": "customer-key", "parser_key": "Customer1",
        "rfq_uuid": "12345678-1234-4234-8234-123456789012", "package_version": "1",
        "documents": [{"document_id": "doc1", "blob_path": "customer-key/rfq-12345678-1234-4234-8234-123456789012/package-1/original-boms/doc1.csv", "size_bytes": 1, "sha256": "a" * 64}],
    }).encode()
    status, response = api.handle(Message(), body)
    assert status == 202
    assert response == {"version": "1", "status": "candidate_required", "candidate": {
        "request_id": "f" * 64, "parser_key": "Customer1", "profile_hash": "e" * 64,
    }}
    assert "blob" not in json.dumps(response).lower()


def test_candidate_required_response_surfaces_author_attempt_exhaustion():
    candidate = CandidateRequiredError("f" * 64, "Customer1", "e" * 64)

    class Processor:
        def process(self, request):
            raise candidate

    class Automation:
        def start_author(self, request_id):
            assert request_id == "f" * 64
            return "exhausted"

    config = ApiConfig("127.0.0.1", 0, "local_disabled", frozenset(), frozenset(), 4096, 1, {})
    api = ProcessingApi(config, Processor, automation_factory=Automation)
    body = json.dumps({
        "job_id": "job1", "customer_key": "customer-key", "parser_key": "Customer1",
        "rfq_uuid": "12345678-1234-4234-8234-123456789012", "package_version": "1",
        "documents": [{"document_id": "doc1", "blob_path": "customer-key/rfq-12345678-1234-4234-8234-123456789012/package-1/original-boms/doc1.csv", "size_bytes": 1, "sha256": "a" * 64}],
    }).encode()

    status, response = api.handle(Message(), body)

    assert status == 202
    assert response["automation_error"] == "author_attempts_exhausted"
