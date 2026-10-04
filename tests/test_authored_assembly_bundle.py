"""Offline existing-agent response integration, not live model evidence.

Run pytest on this file; synthetic inputs/response exercise real persistence.
"""
import json
from types import SimpleNamespace

import pytest

from bom_processor.candidate_authoring import CandidateAuthoringService
from bom_processor.candidate_validation import CandidateValidationService, CandidateValidationError
from bom_processor.candidate_review import CandidateReviewService
from bom_processor.foundry_author import FoundryModelAuthor
from test_source_assembly_association import setup

CODE = 'import argparse\np = argparse.ArgumentParser()\np.add_argument("--input")\np.add_argument("--output")\n'


def authored(parser_code=CODE):
    store, sources, associations, old_service = setup()
    path = "candidates/requests/xlsx-demo.json"
    request = json.loads(store.registry[path])
    request["version"] = "candidate-request.v2"
    store.registry[path] = json.dumps(request).encode()
    seen = []

    class Agent:
        def __init__(self, **kwargs):
            assert kwargs["tools"] == []

        async def run(self, text, *, options):
            assert options == {"max_tokens": 8192}
            seen.append(json.loads(text))
            return SimpleNamespace(output_text=json.dumps({"parser_code": parser_code,
                                                           "assembly_associations": associations}))

    result = CandidateAuthoringService("xlsx-demo", store,
        FoundryModelAuthor(agent_factory=Agent), source_associations=True).author()
    service = CandidateValidationService(result.request_id, result.candidate_id, store, old_service.orchestrator)
    original_start = service.orchestrator.start

    def transfer_checked_start(item):
        from worker.transfer import remote_job
        from test_worker_transfer import env, FakeTransport, fake_run
        values = env()
        values["WORKER_OUTPUT_NAME"] = item.output_name
        _, code = remote_job(values, transport=FakeTransport(), run=fake_run)
        assert code == 0
        return original_start(item)

    service.orchestrator.start = transfer_checked_start
    return store, result, service, seen


def test_one_agent_response_is_persisted_then_independently_loaded_and_reviewed():
    store, authored_result, service, seen = authored()
    assert len(seen) == 1
    assert len(seen[0]["sources"]) == 3  # Exact equivalent sources share a hash.
    assert all(source["inventory"]["coverage"] == "complete" for source in seen[0]["sources"])
    manifest = json.loads(store.candidates[authored_result.manifest_path])
    assert manifest["version"] == "candidate-authoring.v6"
    result = service.validate()
    review = CandidateReviewService(store).get_v3(result.request_id, result.candidate_id, result.validation_id)
    assert len(review["groups"]) == 2 and review["approval_ready"]
    assert service.validation_version == "candidate-validation.v4"


def test_authored_association_bytes_cannot_be_replaced():
    store, result, service, _ = authored()
    manifest = json.loads(store.candidates[result.manifest_path])
    store.candidates[manifest["assembly_associations"]["blob_path"]] = b"{}"
    with pytest.raises(CandidateValidationError, match="ASSOCIATION_ARTIFACT_INTEGRITY"):
        service.validate()


def test_bundle_context_limit_is_checked_before_agent_creation():
    from test_source_assembly_association import workbook
    calls = []
    source = workbook([["short words "*20] for _ in range(1000)])
    prompt = SimpleNamespace(canonical_columns=(), rules=())
    with pytest.raises(ValueError, match="AUTHORING_SOURCE_CONTEXT_LIMIT"):
        FoundryModelAuthor(agent_factory=lambda **kwargs: calls.append(kwargs)).generate_bundle(
            prompt, [{"bytes": source, "sha256": "a"*64, "extension": "xlsx"}])
    assert calls == []


def test_grouped_v6_replay_rechecks_source_hash():
    store, _, service, _ = authored()
    service.validate()
    path = next(iter(store.sources))
    store.sources[path] = b"changed after first validation"
    with pytest.raises(CandidateValidationError, match="SOURCE_INTEGRITY"):
        service.validate()


def test_v6_controller_and_job_terminal_markers_share_retry_namespace(monkeypatch):
    import candidate_job
    from bom_processor import InMemoryStorage
    from bom_processor.candidate_automation import CandidateAutomationService
    store = InMemoryStorage()

    class Starter:
        base_environment = {"BOM_SOURCE_ASSOCIATIONS": "true"}
        def start(self, values):
            monkeypatch.setenv("BOM_SOURCE_ASSOCIATIONS", values["BOM_SOURCE_ASSOCIATIONS"])
            candidate_job._write_attempt_marker(store, values["REQUEST_ID"],
                (values["AUTHOR_ATTEMPT_KIND"], int(values["AUTHOR_ATTEMPT"])), "failed", {})
            return "offline-execution"

    controller = CandidateAutomationService(store, Starter())
    assert controller.start_author("request") == "started"
    assert controller.start_author("request") == "started"
    root = "candidate-automation/request/author/candidate-authoring.v6"
    assert root + "/attempt-2.failed.json" in store.registry
    assert controller.start_author_refinement("request", "candidate") == "started"
    root = "candidate-automation/request/author-refinement/candidate-authoring.v6"
    candidate_job._write_attempt_marker(store, "request", ("author-refinement", 1), "completed", {})
    assert controller.start_author_refinement("request", "candidate") == "started"
    assert root + "/attempt-2.started.json" in store.registry