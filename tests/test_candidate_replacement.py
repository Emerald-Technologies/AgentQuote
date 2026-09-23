import hashlib
import json

import pytest

from bom_processor import InMemoryStorage
from bom_processor.candidate_automation import CandidateAutomationService
from bom_processor.candidate_authoring import AUTHORING_VERSION
from bom_processor.candidate_status import CandidateStatusService


REQUEST_ID = "request-replacement"
PROFILE_HASH = "a" * 64


def put(storage, path, value, *, registry=False):
    data = json.dumps(value, sort_keys=True).encode()
    method = storage.upload_registry_immutable if registry else storage.upload_candidate_immutable
    method(path, data, "application/json")


def request():
    return {
        "version": "candidate-request.v1", "request_id": REQUEST_ID,
        "parser_key": "Customer1", "customer_key": "customer-key",
        "profile_hash": PROFILE_HASH, "source_evidence": [{"document_id": "doc1"}],
        "profile": {}, "status": "requested",
    }


def manifest(candidate_id, version):
    return {
        "version": version, "status": "generated_unvalidated",
        "request_id": REQUEST_ID, "candidate_id": candidate_id,
        "parser_key": "Customer1", "customer_key": "customer-key",
        "profile_hash": PROFILE_HASH, "code_sha256": "b" * 64,
    }


def test_status_selects_new_v3_candidate_instead_of_contained_v2_approval():
    storage = InMemoryStorage()
    put(storage, f"candidates/requests/{REQUEST_ID}.json", request(), registry=True)
    old = "candidate-v2"
    new = "candidate-v3"
    put(storage, f"candidates/{REQUEST_ID}/{old}/candidate-manifest.json",
        manifest(old, "candidate-authoring.v2"))
    put(storage, f"candidates/{REQUEST_ID}/{new}/candidate-manifest.json",
        manifest(new, "candidate-authoring.v3"))
    put(storage, f"candidates/decisions/{REQUEST_ID}/{old}/validation-1.json", {
        "request_id": REQUEST_ID, "candidate_id": old, "decision": "approve",
        "validation_id": "validation-1", "decision_id": "decision-1",
    }, registry=True)

    result = CandidateStatusService(storage).get(REQUEST_ID)

    assert result["candidate_id"] == new
    assert result["status"] == "generated_unvalidated"
    assert "decision_id" not in result


def test_authoring_restarts_when_only_old_policy_candidate_and_marker_exist():
    storage = InMemoryStorage()
    old = "candidate-v2"
    put(storage, f"candidates/{REQUEST_ID}/{old}/candidate-manifest.json",
        manifest(old, "candidate-authoring.v2"))
    put(storage, f"candidate-automation/{REQUEST_ID}/author-started.json", {
        "request_id": REQUEST_ID, "job": "author", "authoring_version": "candidate-authoring.v2",
    }, registry=True)

    class Starter:
        def __init__(self):
            self.values = []

        def start(self, values):
            self.values.append(dict(values))

    starter = Starter()
    result = CandidateAutomationService(storage, author_starter=starter).start_author(REQUEST_ID)

    assert result == "started"
    assert starter.values == [{"REQUEST_ID": REQUEST_ID}]
    marker = json.loads(storage.registry[
        f"candidate-automation/{REQUEST_ID}/author-started-{AUTHORING_VERSION}.json"])
    assert marker["authoring_version"] == AUTHORING_VERSION


def test_failed_start_records_attempt_and_next_call_retries():
    storage = InMemoryStorage()

    class Starter:
        def __init__(self):
            self.calls = 0

        def start(self, values):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("safe failure")
            return "execution-2"

    starter = Starter()
    service = CandidateAutomationService(storage, author_starter=starter)
    with pytest.raises(RuntimeError):
        service.start_author(REQUEST_ID)
    assert service.start_author(REQUEST_ID) == "started"
    prefix = f"candidate-automation/{REQUEST_ID}/author/{AUTHORING_VERSION}"
    assert f"{prefix}/attempt-1.failed.json" in storage.registry
    started = json.loads(storage.registry[f"{prefix}/attempt-2.started.json"])
    assert started["execution_name"] == "execution-2"


def test_existing_claim_suppresses_concurrent_duplicate_start():
    storage = InMemoryStorage()
    prefix = f"candidate-automation/{REQUEST_ID}/author/{AUTHORING_VERSION}"
    put(storage, f"{prefix}/attempt-1.claim.json", {
        "request_id": REQUEST_ID, "job": "author", "attempt": 1,
    }, registry=True)

    class Starter:
        def start(self, values):
            raise AssertionError("concurrent claim must suppress another start")

    assert CandidateAutomationService(storage, author_starter=Starter()).start_author(REQUEST_ID) is None


def test_started_attempt_and_attempt_limit_suppress_additional_starts():
    storage = InMemoryStorage()
    prefix = f"candidate-automation/{REQUEST_ID}/author/{AUTHORING_VERSION}"
    for attempt in range(1, 4):
        put(storage, f"{prefix}/attempt-{attempt}.claim.json", {"attempt": attempt}, registry=True)
        put(storage, f"{prefix}/attempt-{attempt}.failed.json", {"attempt": attempt}, registry=True)

    class Starter:
        def start(self, values):
            raise AssertionError("attempt limit must suppress another start")

    assert CandidateAutomationService(storage, author_starter=Starter()).start_author(REQUEST_ID) is None
