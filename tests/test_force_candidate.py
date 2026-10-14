import hashlib

import pytest

from bom_processor import CandidateRequiredError, InMemoryStorage, ProcessingRequest
from bom_processor.core import BomProcessor
from bom_processor.remote import RemoteBomProcessor


def test_force_candidate_bypasses_existing_manifest_and_registry_parser():
    source_path = "cust/rfq-12345678-1234-4234-8234-123456789012/package-v1/original-boms/doc1-input.csv"
    source = b"Part Number,Qty\nABC-1,2\n"
    request = ProcessingRequest.from_dict({
        "job_id": "replacement-job", "customer_key": "cust", "parser_key": "cust",
        "rfq_uuid": "12345678-1234-4234-8234-123456789012", "package_version": "v1",
        "force_candidate": True,
        "documents": [{"document_id": "doc1", "blob_path": source_path,
                       "size_bytes": len(source), "sha256": hashlib.sha256(source).hexdigest()}],
    })
    storage = InMemoryStorage(sources={source_path: source})
    manifest_path = "cust/rfq-12345678-1234-4234-8234-123456789012/package-v1/processed/replacement-job/manifest.json"
    storage.outputs[manifest_path] = b'{"status":"completed"}'

    with pytest.raises(CandidateRequiredError) as raised:
        BomProcessor(storage).process(request)

    assert raised.value.parser_key == "cust"
    assert list(storage.registry).count(f"candidates/requests/{raised.value.request_id}.json") == 1


def test_remote_force_candidate_bypasses_existing_manifest_and_worker():
    source_path = "cust/rfq-12345678-1234-4234-8234-123456789012/package-v1/original-boms/doc1-input.csv"
    source = b"Part Number,Qty\nABC-1,2\n"
    request = ProcessingRequest.from_dict({
        "job_id": "replacement-job", "customer_key": "cust", "parser_key": "cust",
        "rfq_uuid": "12345678-1234-4234-8234-123456789012", "package_version": "v1",
        "force_candidate": True,
        "documents": [{"document_id": "doc1", "blob_path": source_path,
                       "size_bytes": len(source), "sha256": hashlib.sha256(source).hexdigest()}],
    })
    storage = InMemoryStorage(sources={source_path: source})
    manifest_path = "cust/rfq-12345678-1234-4234-8234-123456789012/package-v1/processed/replacement-job/manifest.json"
    storage.outputs[manifest_path] = b'{"status":"completed"}'

    class NoWorker:
        def start(self, _item):
            raise AssertionError("worker must not start for forced candidate")

    with pytest.raises(CandidateRequiredError):
        RemoteBomProcessor(storage, NoWorker()).process(request)