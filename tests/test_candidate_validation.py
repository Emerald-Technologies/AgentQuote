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