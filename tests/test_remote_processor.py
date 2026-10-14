import hashlib
import io
import json
from datetime import datetime, timezone

import pandas as pd
import pytest

from bom_processor import (
    BlobConflictError,
    InMemoryStorage,
    ProcessingError,
    ProcessingRequest,
    RemoteBomProcessor,
    StartResult,
)
from bom_processor.core import CANONICAL_COLUMNS

RFQ = "12345678-1234-4234-8234-123456789012"
SOURCE_PATH = f"cust/rfq-{RFQ}/package-1/original-boms/doc1-input.xlsx"
PARSER = b"approved parser"


def workbook(**changes):
    row = {column: None for column in CANONICAL_COLUMNS}
    row.update(line_number=1, item_number="P1", item_name="Widget", item_category="Part", quantity=2.0)
    row.update(changes)
    stream = io.BytesIO()
    pd.DataFrame([row], columns=CANONICAL_COLUMNS).to_excel(stream, index=False)
    return stream.getvalue()


def request(source):
    return ProcessingRequest.from_dict({
        "job_id": "job1", "customer_key": "cust", "parser_key": "Parser1",
        "rfq_uuid": RFQ, "package_version": "1", "documents": [{
            "document_id": "doc1", "blob_path": SOURCE_PATH,
            "size_bytes": len(source), "sha256": hashlib.sha256(source).hexdigest(),
        }],
    })


class Orchestrator:
    def __init__(self, storage, output, *, failed=False, bad_hash=False):
        self.storage = storage
        self.output = output
        self.failed = failed
        self.bad_hash = bad_hash
        self.calls = []

    def start(self, item):
        self.calls.append(item)
        output_hash = hashlib.sha256(self.output).hexdigest()
        self.storage.outputs[item.output_blob] = self.output
        result = {
            "contract_version": "remote-worker.v1", "job": item.job_id,
            "status": "failed" if self.failed else "completed",
            "failure": "parser_exit" if self.failed else None,
            "input": {"verified": True, "sha256": item.input_sha256, "size": item.input_size},
            "parser": {"verified": True, "sha256": item.parser_sha256, "size": item.parser_size},
            "output": {
                "sha256": "f" * 64 if self.bad_hash else output_hash,
                "size": len(self.output),
                "content_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            },
        }
        self.storage.outputs[item.result_blob] = json.dumps(result).encode()
        return StartResult(item.job_id, "execution1", datetime.now(timezone.utc))


def processor(source, output=None, **orchestrator_options):
    storage = InMemoryStorage(sources={SOURCE_PATH: source}, parsers={"Parser1/parse.py": PARSER})
    orchestration = Orchestrator(storage, output or source, **orchestrator_options)
    return RemoteBomProcessor(storage, orchestration, sleep=lambda _: None), storage, orchestration


def test_remote_happy_path_preserves_quickquote_manifest_contract():
    source = workbook()
    remote, storage, orchestration = processor(source)
    result = remote.process(request(source))
    assert result["status"] == "completed"
    assert result["results"][0]["output"]["blob_path"] == f"cust/rfq-{RFQ}/package-1/processed/job1/doc1-parsed.xlsx"
    assert orchestration.calls[0].input_name == "input.xlsx"
    assert orchestration.calls[0].job_id == "job1-doc1"
    assert orchestration.calls[0].output_name == "output.xlsx"
    assert orchestration.calls[0].output_prefix.endswith("/worker/job1-doc1")
    assert storage.outputs[f"cust/rfq-{RFQ}/package-1/processed/job1/manifest.json"]


def test_new_namespace_without_registry_or_legacy_parser_requests_candidate():
    from bom_processor import CandidateRequiredError

    source = workbook()
    storage = InMemoryStorage(sources={SOURCE_PATH: source})
    orchestration = Orchestrator(storage, source)
    remote = RemoteBomProcessor(storage, orchestration, sleep=lambda _: None)
    with pytest.raises(CandidateRequiredError) as failure:
        remote.process(request(source))
    assert failure.value.parser_key == "Parser1"
    assert orchestration.calls == []
    assert f"candidates/requests/{failure.value.request_id}.json" in storage.registry


def test_manifest_replay_does_not_start_another_job():
    source = workbook()
    remote, _, orchestration = processor(source)
    first = remote.process(request(source))
    assert remote.process(request(source)) == first
    assert len(orchestration.calls) == 1


def test_worker_failure_and_hash_mismatch_fail_closed():
    source = workbook()
    remote, _, _ = processor(source, failed=True)
    with pytest.raises(ProcessingError, match="worker result schema invalid"):
        remote.process(request(source))
    remote, _, _ = processor(source, bad_hash=True)
    with pytest.raises(ProcessingError, match="worker output hash mismatch"):
        remote.process(request(source))


def test_api_rejects_noncanonical_worker_xlsx():
    source = workbook()
    bad = workbook(item_category="Assembly")
    remote, _, _ = processor(source, output=bad)
    with pytest.raises(ProcessingError, match="item category invalid"):
        remote.process(request(source))


def test_claim_conflict_rejects_reused_job_id():
    source = workbook()
    remote, storage, _ = processor(source)
    prefix = f"cust/rfq-{RFQ}/package-1/processed/job1"
    storage.outputs[f"{prefix}/claim.json"] = json.dumps({"request_fingerprint": "f" * 64}).encode()
    with pytest.raises(BlobConflictError, match="different request"):
        remote.process(request(source))
