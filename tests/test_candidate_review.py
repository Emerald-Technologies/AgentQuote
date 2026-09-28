import hashlib
import io
import json

import pandas as pd
import pytest

from bom_processor import CandidateReviewError, CandidateReviewService, InMemoryStorage
from bom_processor.core import CANONICAL_COLUMNS, OUTPUT_CONTENT_TYPE


def workbook():
    row = {name: None for name in CANONICAL_COLUMNS}
    row.update(line_number=1, item_number="R1", item_name="Resistor",
               item_category="Part", quantity=2,
               manufacturer_1="Yageo", manufacturer_item_number_1="RC0603")
    stream = io.BytesIO()
    pd.DataFrame([row], columns=CANONICAL_COLUMNS).to_excel(stream, index=False, engine="openpyxl")
    return stream.getvalue()


def storage(source_count=1):
    request_id, candidate_id, validation_id = "request-1", "candidate-1", "validation-1"
    source = b"Item Number,Description,Qty,Manufacturer Part Number\nR1,Resistor,2,RC0603\n"
    output = workbook()
    evidence_items = [{"document_id": f"doc-{index}", "blob_path": f"boms/doc-{index}.csv",
                       "size_bytes": len(source), "sha256": hashlib.sha256(source).hexdigest()}
                      for index in range(1, source_count + 1)]
    candidates = {
        f"candidates/{request_id}/{candidate_id}/validation/{validation_id}/evidence.json": json.dumps({
            "request_id": request_id, "candidate_id": candidate_id, "validation_id": validation_id,
            "status": "awaiting_approval", "schema": {"valid": True},
            "fixtures": evidence_items[:1],
            "output": {"blob_path": f"candidates/{request_id}/{candidate_id}/validation/{validation_id}/worker/output.xlsx",
                       "size_bytes": len(output), "sha256": hashlib.sha256(output).hexdigest(),
                       "content_type": OUTPUT_CONTENT_TYPE},
        }).encode(),
        f"candidates/{request_id}/{candidate_id}/validation/{validation_id}/worker/output.xlsx": output,
    }
    registry = {f"candidates/requests/{request_id}.json": json.dumps({
        "request_id": request_id, "source_evidence": evidence_items,
    }).encode()}
    sources = {item["blob_path"]: source for item in evidence_items}
    store = InMemoryStorage(sources=sources, candidates=candidates)
    store.registry.update(registry)
    return store, output


def test_review_binds_output_and_reports_source_vs_parsed_counts():
    store, output = storage()
    summary, actual = CandidateReviewService(store).get("request-1", "candidate-1", "validation-1")
    assert actual == output
    assert summary["source"]["line_items"] == 1
    assert summary["source"]["mpn_cells"] == 1
    assert summary["source"]["distinct_mpns"] == 1
    assert summary["parsed"]["line_items"] == 1
    assert summary["parsed"]["mpn_cells"] == 1
    assert summary["parsed"]["distinct_mpns"] == 1


def test_review_refuses_partial_multi_source_evidence():
    store, _ = storage(source_count=2)
    with pytest.raises(CandidateReviewError, match="REVIEW_INCOMPLETE"):
        CandidateReviewService(store).get("request-1", "candidate-1", "validation-1")


def test_review_refuses_malformed_fixture_without_leaking_an_internal_error():
    store, _ = storage()
    path = "candidates/request-1/candidate-1/validation/validation-1/evidence.json"
    evidence = json.loads(store.candidates[path])
    evidence["fixtures"] = [None]
    store.candidates[path] = json.dumps(evidence).encode()
    with pytest.raises(CandidateReviewError, match="REVIEW_BINDING_INVALID"):
        CandidateReviewService(store).get("request-1", "candidate-1", "validation-1")
