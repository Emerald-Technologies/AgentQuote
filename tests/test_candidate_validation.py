import hashlib
import io
import json
from datetime import datetime, timezone

import pandas as pd
import pytest

from bom_processor import InMemoryStorage, StartResult
from bom_processor.candidate_authoring import CandidateAuthoringService
from bom_processor.candidate_validation import CandidateValidationError, CandidateValidationService
from bom_processor.candidates import persist_candidate
from bom_processor.core import CANONICAL_COLUMNS, OUTPUT_CONTENT_TYPE
from bom_processor.master_registry import profile_bytes

SOURCE_PATH = "customer/rfq-id/package-1/original-boms/doc.csv"
SOURCE = b"mystery,qty\nX,2\n"
HTML_SOURCE_PATH = "customer/rfq-id/package-1/original-boms/doc.xls"
HTML_SOURCE = (b"<html><body><table><tr><th>item_number</th><th>item_name</th><th>quantity</th></tr>"
               b"<tr><td>A-1</td><td>Widget A</td><td>1</td></tr>"
               b"<tr><td>A-2</td><td>Widget B</td><td>2</td></tr>"
               b"<tr><td>A-3</td><td>Widget C</td><td>3</td></tr>"
               b"<tr><td>A-4</td><td>Widget D</td><td>4</td></tr>"
               b"</table></body></html>")
CODE = '''import argparse
parser = argparse.ArgumentParser()
parser.add_argument("--input", required=True)
parser.add_argument("--output", required=True)
args = parser.parse_args()
'''


def workbook(valid=True):
    columns = CANONICAL_COLUMNS if valid else ["wrong"]
    row = {column: None for column in columns}
    if valid:
        row.update(line_number=1, item_number="P1", item_name="Widget",
                   item_category="Part", quantity=2.0)
    stream = io.BytesIO()
    pd.DataFrame([row], columns=columns).to_excel(stream, index=False)
    return stream.getvalue()


def semantic_workbook(rows):
    values = []
    for line_number, (item_number, item_name, quantity) in enumerate(rows, 1):
        row = {column: None for column in CANONICAL_COLUMNS}
        row.update(line_number=line_number, item_number=item_number, item_name=item_name,
                   item_category="Part", quantity=quantity)
        values.append(row)
    stream = io.BytesIO()
    pd.DataFrame(values, columns=CANONICAL_COLUMNS).to_excel(stream, index=False)
    return stream.getvalue()


class Model:
    def generate(self, prompt, source_bytes):
        return CODE


class Orchestrator:
    def __init__(self, storage, output):
        self.storage, self.output = storage, output
        self.calls = []

    def start(self, item):
        self.calls.append(item)
        self.storage.candidates[item.output_blob] = self.output
        result = {
            "contract_version": "remote-worker.v1", "job": item.job_id,
            "status": "completed",
            "input": {"verified": True, "sha256": item.input_sha256, "size": item.input_size},
            "parser": {"verified": True, "sha256": item.parser_sha256, "size": item.parser_size},
            "output": {"sha256": hashlib.sha256(self.output).hexdigest(),
                       "size": len(self.output), "content_type": OUTPUT_CONTENT_TYPE},
        }
        self.storage.candidates[item.result_blob] = json.dumps(result).encode()
        return StartResult(item.job_id, "execution-1", datetime.now(timezone.utc))


def setup_validation(output=None):
    storage = InMemoryStorage(sources={SOURCE_PATH: SOURCE})
    profile = profile_bytes(SOURCE, ".csv")
    request = persist_candidate(storage, "Customer1", "customer-key", profile, ({
        "document_id": "doc1", "blob_path": SOURCE_PATH,
        "size_bytes": len(SOURCE), "sha256": hashlib.sha256(SOURCE).hexdigest(),
    },))
    candidate = CandidateAuthoringService(request.request_id, storage, Model()).author()
    orchestrator = Orchestrator(storage, output or workbook())
    service = CandidateValidationService(
        request.request_id, candidate.candidate_id, storage, orchestrator,
        job_resource="subscriptions/sub/resourceGroups/rg/providers/Microsoft.App/jobs/worker",
        worker_image="registry/worker@sha256:" + "a" * 64, sleep=lambda _: None)
    return service, storage, orchestrator, request, candidate


def setup_html_validation(output):
    storage = InMemoryStorage(sources={HTML_SOURCE_PATH: HTML_SOURCE})
    profile = profile_bytes(HTML_SOURCE, ".xls")
    request = persist_candidate(storage, "Customer1", "customer-key", profile, ({
        "document_id": "doc1", "blob_path": HTML_SOURCE_PATH,
        "size_bytes": len(HTML_SOURCE), "sha256": hashlib.sha256(HTML_SOURCE).hexdigest(),
    },))
    candidate = CandidateAuthoringService(request.request_id, storage, Model()).author()
    orchestrator = Orchestrator(storage, output)
    service = CandidateValidationService(
        request.request_id, candidate.candidate_id, storage, orchestrator,
        job_resource="subscriptions/sub/resourceGroups/rg/providers/Microsoft.App/jobs/worker",
        worker_image="registry/worker@sha256:" + "a" * 64, sleep=lambda _: None)
    return service, storage, orchestrator, request, candidate


def test_valid_candidate_uses_worker_and_records_immutable_transition():
    service, storage, orchestrator, request, candidate = setup_validation()
    result = service.validate()
    assert result.status == "awaiting_approval"
    assert len(orchestrator.calls) == 1
    item = orchestrator.calls[0]
    assert item.parser_blob == f"candidates/{request.request_id}/{candidate.candidate_id}/parse.py"
    assert item.output_container == "parsers"
    evidence = json.loads(storage.candidates[result.evidence_path])
    assert evidence["candidate"]["sha256"] == hashlib.sha256(CODE.encode()).hexdigest()
    assert evidence["fixtures"][0]["sha256"] == hashlib.sha256(SOURCE).hexdigest()
    assert evidence["output"]["sha256"] == hashlib.sha256(orchestrator.output).hexdigest()
    assert evidence["schema"] == {"valid": True, "result": "valid"}
    assert evidence["worker"]["execution_name"] == "execution-1"
    event = f"candidates/events/{request.request_id}/{candidate.candidate_id}/validated-{result.validation_id}.json"
    assert json.loads(storage.candidates[event])["status"] == "awaiting_approval"
    assert all("approved/" not in path and "registry/" not in path for path in storage.candidates)


def test_validation_replay_does_not_start_second_worker():
    service, _, orchestrator, _, _ = setup_validation()
    first = service.validate()
    assert service.validate() == first
    assert len(orchestrator.calls) == 1


def test_html_fallback_row_is_semantically_invalid_and_not_validated():
    output = semantic_workbook([("UNKNOWN", "Fallback", 1)])
    service, storage, orchestrator, _, _ = setup_html_validation(output)
    with pytest.raises(CandidateValidationError, match="SEMANTIC_INVALID"):
        service.validate()
    assert len(orchestrator.calls) == 1
    evidence_paths = [path for path in storage.candidates if path.endswith("/evidence.json")]
    assert len(evidence_paths) == 1
    evidence = json.loads(storage.candidates[evidence_paths[0]])
    assert evidence["status"] == "generated_unvalidated"
    assert evidence["semantic"]["valid"] is False
    assert not any("validated-" in path for path in storage.candidates)


def test_html_rows_with_source_identities_are_semantically_validated():
    output = semantic_workbook([("A-1", "Widget A", 0.025), ("A-3", "Widget C", 3)])
    service, storage, orchestrator, _, _ = setup_html_validation(output)
    result = service.validate()
    assert result.status == "awaiting_approval"
    assert len(orchestrator.calls) == 1
    evidence = json.loads(storage.candidates[result.evidence_path])
    assert evidence["semantic"]["valid"] is True
    assert evidence["semantic"]["result"] == "valid"
    event_paths = [path for path in storage.candidates if "validated-" in path]
    assert len(event_paths) == 1
    assert json.loads(storage.candidates[event_paths[0]])["status"] == "awaiting_approval"


def test_candidate_hash_mismatch_fails_before_worker_start():
    service, storage, orchestrator, request, candidate = setup_validation()
    path = f"candidates/{request.request_id}/{candidate.candidate_id}/parse.py"
    storage.candidates[path] += b"# changed"
    with pytest.raises(CandidateValidationError, match="CANDIDATE_INTEGRITY"):
        service.validate()
    assert orchestrator.calls == []


def test_fixture_hash_mismatch_fails_before_worker_start():
    service, storage, orchestrator, _, _ = setup_validation()
    storage.sources[SOURCE_PATH] = b"changed"
    with pytest.raises(CandidateValidationError, match="SOURCE_INTEGRITY"):
        service.validate()
    assert orchestrator.calls == []


def test_schema_failure_records_evidence_without_transition():
    service, storage, orchestrator, request, candidate = setup_validation(workbook(False))
    with pytest.raises(CandidateValidationError, match="SCHEMA_INVALID"):
        service.validate()
    assert len(orchestrator.calls) == 1
    evidence_paths = [path for path in storage.candidates if path.endswith("/evidence.json")]
    assert len(evidence_paths) == 1
    evidence = json.loads(storage.candidates[evidence_paths[0]])
    assert evidence["status"] == "generated_unvalidated"
    assert evidence["schema"]["valid"] is False
    assert not any("validated-" in path for path in storage.candidates)
    assert all("approved/" not in path and "registry/" not in path for path in storage.candidates)


def test_worker_output_hash_mismatch_fails_closed():
    service, storage, orchestrator, _, _ = setup_validation()
    original_start = orchestrator.start

    def corrupt(item):
        result = original_start(item)
        value = json.loads(storage.candidates[item.result_blob])
        value["output"]["sha256"] = "f" * 64
        storage.candidates[item.result_blob] = json.dumps(value).encode()
        return result

    orchestrator.start = corrupt
    with pytest.raises(CandidateValidationError, match="WORKER_OUTPUT_INTEGRITY"):
        service.validate()
    assert not any(path.endswith("/evidence.json") for path in storage.candidates)


def test_grouped_validation_pass_fail_and_existing_result_replay():
    second_path = "customer/rfq-id/package-1/original-boms/doc-2.csv"
    second = b"mystery,qty\nY,3\n"
    storage = InMemoryStorage(sources={SOURCE_PATH: SOURCE, second_path: second})
    profile = profile_bytes(SOURCE, ".csv")
    request = persist_candidate(storage, "Customer1", "customer-key", profile, ({
        "document_id": "doc1", "blob_path": SOURCE_PATH, "size_bytes": len(SOURCE),
        "sha256": hashlib.sha256(SOURCE).hexdigest(),
    }, {
        "document_id": "doc2", "blob_path": second_path, "size_bytes": len(second),
        "sha256": hashlib.sha256(second).hexdigest(),
    }))
    candidate = CandidateAuthoringService(request.request_id, storage, Model()).author()
    orchestrator = Orchestrator(storage, workbook())
    service = CandidateValidationService(request.request_id, candidate.candidate_id, storage,
                                         orchestrator, sleep=lambda _: None)
    first = service.validate()
    assert first.status == "awaiting_approval"
    assert service.validate() == first
    assert len(orchestrator.calls) == 2
    evidence = json.loads(storage.candidates[first.evidence_path])
    assert evidence["complete"] is True
    assert all(item["status"] == "passed" for item in evidence["fixtures"])

    evidence["fixtures"][1]["status"] = "failed"
    evidence["status"] = "generated_unvalidated"
    storage.candidates[first.evidence_path] = json.dumps(evidence).encode()
    replay = service.validate()
    assert replay.status == "generated_unvalidated"
    assert len(orchestrator.calls) == 2


def test_grouped_validation_fails_aggregate_when_one_fixture_fails():
    second_path = "customer/rfq-id/package-1/original-boms/doc-2.csv"
    second = b"mystery,qty\nY,3\n"
    storage = InMemoryStorage(sources={SOURCE_PATH: SOURCE, second_path: second})
    request = persist_candidate(storage, "Customer1", "customer-key", profile_bytes(SOURCE, ".csv"), ({
        "document_id": "doc1", "blob_path": SOURCE_PATH, "size_bytes": len(SOURCE),
        "sha256": hashlib.sha256(SOURCE).hexdigest(),
    }, {
        "document_id": "doc2", "blob_path": second_path, "size_bytes": len(second),
        "sha256": hashlib.sha256(second).hexdigest(),
    }))
    candidate = CandidateAuthoringService(request.request_id, storage, Model()).author()

    class MixedOrchestrator(Orchestrator):
        def start(self, item):
            self.output = workbook() if not self.calls else workbook(False)
            return super().start(item)

    orchestrator = MixedOrchestrator(storage, workbook())
    result = CandidateValidationService(request.request_id, candidate.candidate_id, storage,
                                        orchestrator, sleep=lambda _: None).validate()
    evidence = json.loads(storage.candidates[result.evidence_path])
    assert result.status == "generated_unvalidated"
    assert evidence["complete"] is True
    assert [item["status"] for item in evidence["fixtures"]] == ["passed", "failed"]
    assert not any("validated-" in path for path in storage.candidates)


def failed_worker(item, *, message="parser failed | ValueError: no supported source rows found",
                  failure="transfer_failed"):
    return {"contract_version": "remote-worker.v1", "job": item.job_id, "status": "failed",
            "input": {"verified": True, "sha256": item.input_sha256, "size": item.input_size},
            "parser": {"verified": True, "sha256": item.parser_sha256, "size": item.parser_size},
            "output": {"sha256": None, "size": 0}, "failure": failure,
            "failure_classification": {"error_type": "RuntimeError", "error_message": message}}


def test_verified_failed_worker_does_not_require_output_and_preserves_feedback():
    service, storage, orchestrator, _, _ = setup_validation()
    def start(item):
        orchestrator.calls.append(item)
        storage.candidates[item.result_blob] = json.dumps(failed_worker(item)).encode()
        return StartResult(item.job_id, "execution-failed", datetime.now(timezone.utc))
    orchestrator.start = start
    with pytest.raises(CandidateValidationError, match="WORKER_PARSER_FAILED"):
        service.validate()
    event = json.loads(next(data for path, data in storage.candidates.items() if "validation-failed-" in path))
    assert event["repairable"] is True
    assert "no supported source rows found" in event["failure_message"]
    assert not any(path.endswith("/output.xlsx") for path in storage.candidates)


@pytest.mark.parametrize("mutation,code", [
    ({"job": "wrong"}, "WORKER_RESULT_INVALID"),
    ({"status": "unknown"}, "WORKER_RESULT_INVALID"),
    ({"input": {"verified": False}}, "WORKER_EVIDENCE_INVALID"),
    ({"parser": {"verified": True, "sha256": "0" * 64, "size": 1}}, "WORKER_EVIDENCE_INVALID"),
])
def test_failed_worker_unverified_identity_cannot_supply_repair_feedback(mutation, code):
    service, storage, orchestrator, _, _ = setup_validation()
    def start(item):
        value = failed_worker(item)
        value.update(mutation)
        storage.candidates[item.result_blob] = json.dumps(value).encode()
        return StartResult(item.job_id, "execution", datetime.now(timezone.utc))
    orchestrator.start = start
    with pytest.raises(CandidateValidationError, match=code):
        service.validate()
    event = json.loads(next(data for path, data in storage.candidates.items() if "validation-failed-" in path))
    assert event["repairable"] is False
    assert event["failure_message"] is None


def test_grouped_three_fixture_failure_event_reaches_refinement_once():
    from bom_processor.candidate_automation import CandidateAutomationService
    from bom_processor.candidate_status import CandidateStatusService
    evidence = []
    storage = InMemoryStorage()
    for index, data in enumerate((SOURCE, b"mystery,qty\nY,3\n", b"mystery,qty\nZ,4\n")):
        path = f"customer/rfq-id/package-1/original-boms/doc-{index}.csv"
        storage.sources[path] = data
        evidence.append({"document_id": f"doc{index}", "blob_path": path,
                         "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    evidence.append({**evidence[0], "document_id": "duplicate"})
    request = persist_candidate(storage, "Customer1", "customer-key", profile_bytes(SOURCE, ".csv"), tuple(evidence))
    candidate = CandidateAuthoringService(request.request_id, storage, Model()).author()
    class FailedOrchestrator:
        calls = []
        def start(self, item):
            self.calls.append(item)
            storage.candidates[item.result_blob] = json.dumps(failed_worker(item)).encode()
            return StartResult(item.job_id, "failed-execution", datetime.now(timezone.utc))
    orchestrator = FailedOrchestrator()
    service = CandidateValidationService(request.request_id, candidate.candidate_id, storage, orchestrator)
    result = service.validate()
    assert service.validate() == result and len(orchestrator.calls) == 3
    aggregate = json.loads(storage.candidates[result.evidence_path])
    assert sorted(len(f["source_aliases"]) for f in aggregate["fixtures"]) == [1, 1, 2]
    events = [json.loads(data) for path, data in storage.candidates.items() if "validation-failed-" in path]
    assert len(events) == 1 and events[0]["repairable"] is True
    assert events[0]["evidence_sha256"] == hashlib.sha256(storage.candidates[result.evidence_path]).hexdigest()
    assert len(events[0]["fixtures"]) == 3
    event_path = next(path for path in storage.candidates if "validation-failed-" in path)
    saved_event = storage.candidates.pop(event_path)
    assert service.validate() == result
    assert storage.candidates[event_path] == saved_event
    class Starter:
        calls = []
        def start(self, values):
            self.calls.append(values)
            return "refinement-execution"
    starter = Starter()
    status = CandidateStatusService(storage, automation=CandidateAutomationService(storage, author_starter=starter))
    status.get(request.request_id)
    status.get(request.request_id)
    assert len(starter.calls) == 1
    assert "no supported source rows found" in starter.calls[0]["AUTHOR_FEEDBACK"]


def test_grouped_infrastructure_failure_holds_without_author_start():
    from bom_processor.candidate_status import CandidateStatusService
    service, storage, orchestrator, request, _ = setup_validation()
    payload = json.loads(storage.registry[f"candidates/requests/{request.request_id}.json"])
    payload["source_evidence"].append({**payload["source_evidence"][0], "document_id": "alias"})
    storage.registry[f"candidates/requests/{request.request_id}.json"] = json.dumps(payload).encode()
    def start(item):
        storage.candidates[item.result_blob] = json.dumps(failed_worker(item, message="output transfer failed")).encode()
        return StartResult(item.job_id, "failed", datetime.now(timezone.utc))
    orchestrator.start = start
    service.validate()
    class Automation:
        def start_author_refinement(self, *args):
            pytest.fail("infrastructure failure must not call author")
    result = CandidateStatusService(storage, automation=Automation()).get(request.request_id)
    assert result["automation_error"] == "validation_failure_requires_review"
    # Interrupted/unavailable event publication must not invoke the old fallback.
    event_path = next(path for path in storage.candidates if "validation-failed-" in path)
    storage.candidates.pop(event_path)
    assert CandidateStatusService(storage, automation=Automation()).get(request.request_id)["automation_error"] == "validation_failure_requires_review"


def test_failure_feedback_redacts_credentials_urls_and_paths():
    value = {"failure_classification": {"error_type": "RuntimeError",
        "error_message": "parser failed | https://example.test/a?sig=hidden C:\\private\\file token=hidden password=hidden"}}
    classification = CandidateValidationService._extract_failure_classification(json.dumps(value).encode())
    assert "hidden" not in classification["error_message"]
    assert "example.test" not in classification["error_message"]
    assert len(classification["error_message"]) <= 500


@pytest.mark.parametrize("message", ["Authorization: Bearer secretvalue", 'password="two word secretvalue"',
                                   "Bearer secretvalue", "{'password': 'two word secretvalue'}",
                                   '{"token": "secretvalue"}'])
def test_credential_feedback_is_withheld_completely(message):
    value = {"failure_classification": {"error_type": "RuntimeError", "error_message": message}}
    result = CandidateValidationService._extract_failure_classification(json.dumps(value).encode())
    assert result["error_message"] == "sensitive feedback withheld"


def test_detail_free_parser_failure_is_repairable():
    service, storage, orchestrator, _, _ = setup_validation()
    def start(item):
        storage.candidates[item.result_blob] = json.dumps(failed_worker(item, message="parser failed")).encode()
        return StartResult(item.job_id, "failed", datetime.now(timezone.utc))
    orchestrator.start = start
    with pytest.raises(CandidateValidationError, match="WORKER_PARSER_FAILED"):
        service.validate()


def test_legacy_singleton_evidence_replays_without_new_identity():
    service, storage, orchestrator, _, _ = setup_validation()
    result = service.validate()
    payload = json.loads(storage.candidates[result.evidence_path])
    payload.pop("failure_feedback_policy")
    storage.candidates[result.evidence_path] = json.dumps(payload).encode()
    assert service.validate() == result
    assert len(orchestrator.calls) == 1