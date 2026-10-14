"""Service revalidation of worker artifacts, using synthetic bytes only."""
import copy
import hashlib
import json

import pytest

from bom_processor import InMemoryStorage
from bom_processor.core import OUTPUT_CONTENT_TYPE
from bom_processor.job_orchestrator import WorkItem
from bom_processor.candidate_validation import CandidateValidationService, CandidateValidationError
from bom_processor.interpretation_coverage import verify_coverage
from test_interpretation_coverage import fixture


def context(blocking=False):
    source, output, proposal = fixture()
    if blocking:
        proposal["findings"] = [{"code": "UNRESOLVED", "severity": "blocking", "rationale": "Unclear grouping.", "cells": []}]
    sidecar = json.dumps(proposal).encode()
    digest = lambda data: hashlib.sha256(data).hexdigest()
    item = WorkItem("job", "boms", "parsers", "parsers", "source", "parser",
                    digest(source), len(source), "a"*64, 1, "attempt/job",
                    input_name="input.xlsx", output_name="output.xlsx", interpretation_name="interpretation.json")
    storage = InMemoryStorage()
    storage.sources[item.source_blob] = source
    storage.upload_candidate_immutable(item.output_blob, output, OUTPUT_CONTENT_TYPE)
    storage.upload_candidate_immutable(item.interpretation_blob, sidecar, "application/json")
    result = {"contract_version": "remote-worker.v2", "job": "job", "status": "completed",
              "input": {"verified": True, "sha256": digest(source), "size": len(source)},
              "parser": {"verified": True, "sha256": "a"*64, "size": 1},
              "output": {"sha256": digest(output), "size": len(output), "content_type": OUTPUT_CONTENT_TYPE},
              "interpretation": {"sha256": digest(sidecar), "size": len(sidecar), "content_type": "application/json",
                                 "verification": verify_coverage(source, ".xlsx", output, sidecar)}}
    service = CandidateValidationService("request", "candidate", storage, None)
    return service, storage, item, result


def test_service_independently_binds_exact_sidecar_path_and_values():
    service, _, item, result = context()
    verified = service._verify_worker_result(json.dumps(result).encode(), item)
    assert verified["interpretation"]["blob_path"] == item.interpretation_blob
    assert verified["interpretation"]["verification"]["semantic_acceptance"] == "not_evaluated"


@pytest.mark.parametrize("mutation, code", [
    (lambda r: r.update(contract_version="remote-worker.v1"), "WORKER_RESULT_INVALID"),
    (lambda r: r["interpretation"].update(size=True), "WORKER_INTERPRETATION_INVALID"),
    (lambda r: r["interpretation"].update(sha256="0"*64), "WORKER_INTERPRETATION_INTEGRITY"),
    (lambda r: r["interpretation"]["verification"].update(automatic_acceptance_authorized=True), "WORKER_INTERPRETATION_CLAIM_MISMATCH"),
])
def test_tampered_worker_claims_rejected(mutation, code):
    service, _, item, result = context(); mutation(result)
    with pytest.raises(CandidateValidationError, match=code):
        service._verify_worker_result(json.dumps(result).encode(), item)


def test_blocking_interpretation_cannot_pass_worker_result_gate():
    service, _, item, result = context(blocking=True)
    with pytest.raises(CandidateValidationError, match="WORKER_INTERPRETATION_REVIEW_REQUIRED"):
        service._verify_worker_result(json.dumps(result).encode(), item)