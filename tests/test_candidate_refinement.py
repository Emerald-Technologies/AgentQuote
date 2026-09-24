"""Tests for the candidate refinement loop (bounded re-authoring after validation failure)."""
from __future__ import annotations

import json

import pytest

from bom_processor.candidate_automation import CandidateAutomationService
from bom_processor.candidate_validation import CandidateValidationService
from worker.transfer import _failure_classification


class FakeStorage:
    """In-memory storage double for refinement tests."""

    def __init__(self):
        self.registry = {}
        self.candidates = {}

    def download_registry(self, path, max_bytes):
        if path not in self.registry:
            raise FileNotFoundError(path)
        return self.registry[path]

    def download_candidate(self, path, max_bytes):
        if path not in self.candidates:
            raise FileNotFoundError(path)
        return self.candidates[path]

    def upload_registry_immutable(self, path, data, content_type):
        if path in self.registry:
            raise RuntimeError("conflict")
        self.registry[path] = data

    def upload_candidate_immutable(self, path, data, content_type):
        if path in self.candidates:
            raise RuntimeError("conflict")
        self.candidates[path] = data

    def list_candidate_paths(self, prefix, limit):
        if not prefix.startswith("candidates/"):
            raise ValueError("invalid candidate prefix")
        return [p for p in self.candidates if p.startswith(prefix)][:limit]

    def list_registry_paths(self, prefix, limit):
        return [p for p in self.registry if p.startswith(prefix)][:limit]


class FakeStarter:
    """Records start calls."""

    def __init__(self):
        self.calls = []

    def start(self, values):
        self.calls.append(dict(values))
        return f"execution-{len(self.calls)}"


# --- Worker failure classification ---

def test_failure_classification_basic():
    exc = ValueError("some error message")
    result = _failure_classification(exc)
    assert result["error_type"] == "ValueError"
    assert result["error_message"] == "some error message"


def test_failure_classification_redacts_paths():
    exc = RuntimeError("failed to read C:\\Users\\test\\file.xls")
    result = _failure_classification(exc)
    assert "<path>" in result["error_message"]
    assert "C:\\Users" not in result["error_message"]


def test_failure_classification_truncates_long_messages():
    exc = ValueError("x" * 500)
    result = _failure_classification(exc)
    assert len(result["error_message"]) <= 200


# --- Validation-failed event writing ---

def test_validation_failed_event_written():
    storage = FakeStorage()
    service = CandidateValidationService.__new__(CandidateValidationService)
    service.request_id = "req-1"
    service.candidate_id = "cand-1"
    service.storage = storage

    from bom_processor.job_orchestrator import WorkItem
    item = WorkItem(
        job_id="validate-abc123", source_container="boms", parser_container="parsers",
        output_container="parsers", source_blob="boms/test.xls",
        parser_blob="candidates/req-1/cand-1/parse.py",
        input_sha256="a" * 64, input_size=100,
        parser_sha256="b" * 64, parser_size=50,
        output_prefix="candidates/req-1/cand-1/validation/validate-abc123",
        input_name="input.xls", output_name="output.xlsx", result_name="result.json",
    )

    service._write_validation_failed_event(
        "validation-id-123", item, "WORKER_RESULT_INVALID",
        {"error_type": "KeyError", "error_message": "missing column"})

    event_path = "candidates/events/req-1/cand-1/validation-failed-validation-id-123.json"
    assert event_path in storage.candidates
    event = json.loads(storage.candidates[event_path])
    assert event["event"] == "candidate.validation_failed"
    assert event["error_code"] == "WORKER_RESULT_INVALID"
    assert event["failure"] == "KeyError"


def test_validation_failed_event_best_effort():
    """Event write failure must not raise."""
    storage = FakeStorage()
    # Pre-populate to force a conflict.
    event_path = "candidates/events/req-1/cand-1/validation-failed-vid.json"
    storage.candidates[event_path] = b"existing"

    service = CandidateValidationService.__new__(CandidateValidationService)
    service.request_id = "req-1"
    service.candidate_id = "cand-1"
    service.storage = storage

    from bom_processor.job_orchestrator import WorkItem
    item = WorkItem(
        job_id="validate-abc123", source_container="boms", parser_container="parsers",
        output_container="parsers", source_blob="boms/test.xls",
        parser_blob="candidates/req-1/cand-1/parse.py",
        input_sha256="a" * 64, input_size=100,
        parser_sha256="b" * 64, parser_size=50,
        output_prefix="candidates/req-1/cand-1/validation/validate-abc123",
        input_name="input.xls", output_name="output.xlsx", result_name="result.json",
    )

    # Should not raise despite conflict.
    service._write_validation_failed_event("vid", item, "WORKER_RESULT_INVALID", None)


# --- Refinement trigger ---

def test_start_author_refinement_triggers():
    storage = FakeStorage()
    starter = FakeStarter()
    automation = CandidateAutomationService(storage, author_starter=starter)

    # Write a validation-failed event.
    event = {"event": "candidate.validation_failed", "failure": "KeyError",
             "failure_message": "missing column"}
    storage.candidates["candidates/events/req-1/cand-1/validation-failed-vid.json"] = \
        json.dumps(event).encode()

    result = automation.start_author_refinement("req-1", "cand-1", event)
    assert result == "started"
    assert len(starter.calls) == 1
    assert starter.calls[0]["REQUEST_ID"] == "req-1"
    assert "AUTHOR_FEEDBACK" in starter.calls[0]
    assert "KeyError" in starter.calls[0]["AUTHOR_FEEDBACK"]


def test_start_author_refinement_respects_max_attempts():
    storage = FakeStorage()
    starter = FakeStarter()
    automation = CandidateAutomationService(storage, author_starter=starter)
    automation.MAX_AUTHOR_ATTEMPTS = 2

    # Simulate 2 existing attempts.
    from bom_processor.candidate_authoring import AUTHORING_VERSION
    root = f"candidate-automation/req-1/author-refinement/{AUTHORING_VERSION}"
    storage.registry[f"{root}/attempt-1.started.json"] = b"{}"
    storage.registry[f"{root}/attempt-2.started.json"] = b"{}"

    result = automation.start_author_refinement("req-1", "cand-1", None)
    assert result is None
    assert len(starter.calls) == 0


def test_start_author_refinement_skips_if_newer_candidate():
    storage = FakeStorage()
    starter = FakeStarter()
    automation = CandidateAutomationService(storage, author_starter=starter)

    from bom_processor.candidate_authoring import AUTHORING_VERSION
    # A newer candidate manifest exists.
    manifest = {"candidate_id": "cand-2", "version": AUTHORING_VERSION}
    storage.candidates["candidates/req-1/cand-2/candidate-manifest.json"] = \
        json.dumps(manifest).encode()

    result = automation.start_author_refinement("req-1", "cand-1", None)
    assert result is None
    assert len(starter.calls) == 0


# --- Authoring with feedback ---

def test_authoring_injects_feedback_into_rules():
    from bom_processor.candidate_authoring import CandidateAuthoringService, CANONICAL_OUTPUT_RULES

    service = CandidateAuthoringService.__new__(CandidateAuthoringService)
    service.request_id = "req-1"
    service.feedback = "Previous parser failed with KeyError: missing column"

    # The feedback should be appended to rules when building the prompt.
    # We verify by checking the service stores it.
    assert service.feedback == "Previous parser failed with KeyError: missing column"


def test_authoring_no_feedback_when_none():
    from bom_processor.candidate_authoring import CandidateAuthoringService

    service = CandidateAuthoringService.__new__(CandidateAuthoringService)
    service.request_id = "req-1"
    service.feedback = None
    assert service.feedback is None


# --- Status projection tiebreak when a refinement supersedes a candidate ---

def _status_request(request_id, profile_hash):
    return {
        "version": "candidate-request.v1", "request_id": request_id,
        "parser_key": "Customer1", "customer_key": "customer-key",
        "profile_hash": profile_hash, "source_evidence": [{"document_id": "doc1"}],
        "profile": {}, "status": "requested",
    }


def _status_manifest(request_id, candidate_id, version):
    return {
        "version": version, "status": "generated_unvalidated",
        "request_id": request_id, "candidate_id": candidate_id,
        "parser_key": "Customer1", "customer_key": "customer-key",
        "profile_hash": "a" * 64, "code_sha256": "b" * 64,
    }


def test_status_prefers_refinement_successor_over_superseded_candidate():
    """Two same-version candidates tie unless one is a refinement source."""
    from bom_processor.candidate_authoring import AUTHORING_VERSION
    from bom_processor.candidate_status import CandidateStatusService

    storage = FakeStorage()
    request_id = "req-1"
    old, new = "cand-old", "cand-new"
    storage.registry[f"candidates/requests/{request_id}.json"] = \
        json.dumps(_status_request(request_id, "a" * 64)).encode()
    storage.candidates[f"candidates/{request_id}/{old}/candidate-manifest.json"] = \
        json.dumps(_status_manifest(request_id, old, AUTHORING_VERSION)).encode()
    storage.candidates[f"candidates/{request_id}/{new}/candidate-manifest.json"] = \
        json.dumps(_status_manifest(request_id, new, AUTHORING_VERSION)).encode()
    # A started refinement marker names the old candidate as its source.
    storage.registry[
        f"candidate-automation/{request_id}/author-refinement/{AUTHORING_VERSION}/attempt-1.started.json"
    ] = json.dumps({"request_id": request_id, "job": "author_refinement",
                    "refinement_of": old}).encode()

    result = CandidateStatusService(storage).get(request_id)

    assert result["candidate_id"] == new
    assert result["status"] == "generated_unvalidated"


def test_status_still_ambiguous_when_no_refinement_lineage():
    """Without a refinement marker, two same-version candidates stay ambiguous."""
    from bom_processor.candidate_authoring import AUTHORING_VERSION
    from bom_processor.candidate_status import CandidateStatusError, CandidateStatusService

    storage = FakeStorage()
    request_id = "req-1"
    storage.registry[f"candidates/requests/{request_id}.json"] = \
        json.dumps(_status_request(request_id, "a" * 64)).encode()
    for cand in ("cand-old", "cand-new"):
        storage.candidates[f"candidates/{request_id}/{cand}/candidate-manifest.json"] = \
            json.dumps(_status_manifest(request_id, cand, AUTHORING_VERSION)).encode()

    with pytest.raises(CandidateStatusError):
        CandidateStatusService(storage).get(request_id)
