"""Round 8 (A040, Nick 2026-10-11): manufacturer data rules for parsed BOMs.

- A BOM with part lines but not one complete manufacturer/MPN pair stops for a person: validation
  fails as not repairable and the status names manufacturer_data_review_required; no automatic
  attempt follows.
- A lone manufacturer or lone MPN is kept as found; one complete pair anywhere is enough.
- A malformed row map gets feedback naming the expected shape; the authoring rules say so too.
Run: python -m pytest tests/test_manufacturer_data_review.py
"""
import hashlib
import io
import json

import pandas as pd
import pytest

from bom_processor import InMemoryStorage
from bom_processor.candidate_authoring import CANONICAL_OUTPUT_RULES, ROW_MAP_RULE, CandidateAuthoringService
from bom_processor.candidate_status import CandidateStatusService
from bom_processor.candidate_validation import (CandidateValidationError, CandidateValidationService,
                                                _manufacturer_pairs, _row_feedback)
from bom_processor.candidates import persist_candidate
from bom_processor.core import CANONICAL_COLUMNS
from bom_processor.master_registry import profile_bytes
from test_candidate_validation import Model, Orchestrator

SOURCE_PATH = "customer/rfq-id/package-1/original-boms/doc.csv"


def output(rows):
    values = []
    for number, extra in enumerate(rows, 1):
        row = {column: None for column in CANONICAL_COLUMNS}
        row.update(line_number=number, item_number=f"P{number}", item_name=f"Widget {number}",
                   item_category="Part", quantity=float(number), **extra)
        values.append(row)
    stream = io.BytesIO()
    pd.DataFrame(values, columns=CANONICAL_COLUMNS).to_excel(stream, index=False)
    return stream.getvalue()


def validate(source, parsed):
    storage = InMemoryStorage(sources={SOURCE_PATH: source})
    request = persist_candidate(storage, "Customer1", "customer-key", profile_bytes(source, ".csv"), ({
        "document_id": "doc1", "blob_path": SOURCE_PATH, "size_bytes": len(source),
        "sha256": hashlib.sha256(source).hexdigest()},))
    candidate = CandidateAuthoringService(request.request_id, storage, Model()).author()
    service = CandidateValidationService(request.request_id, candidate.candidate_id, storage,
                                         Orchestrator(storage, parsed), sleep=lambda _: None)
    return storage, request, service


def test_part_lines_without_any_complete_pair_stop_for_a_person():
    source = b"mystery,name,qty\nP1,Widget 1,1\nP2,Widget 2,2\n"
    storage, request, service = validate(source, output([{}, {}]))
    with pytest.raises(CandidateValidationError, match="SEMANTIC_INVALID"):
        service.validate()
    evidence = json.loads(next(value for path, value in storage.candidates.items() if path.endswith("/evidence.json")))
    assert evidence["status"] == "generated_unvalidated"
    assert evidence["semantic"]["result"] == "manufacturer_data_missing"
    assert evidence["semantic"]["manufacturer_pairs"] == {"part_lines": 2, "complete_pair_lines": 0}
    event = json.loads(next(value for path, value in storage.candidates.items() if "/validation-failed-" in path))
    assert event["repairable"] is False and event["review_reason"] == "manufacturer_data_review_required"

    class Automation:
        def start_author_refinement(self, *args):
            pytest.fail("a person-review stop must not start another author attempt")

    status = CandidateStatusService(storage, automation=Automation()).get(request.request_id)
    assert status["automation_error"] == "manufacturer_data_review_required"


def test_lone_halves_are_kept_when_one_complete_pair_exists():
    source = (b"mystery,name,qty,maker,maker part\n"
              b"P1,Widget 1,1,Acme,A-1\nP2,Widget 2,2,Brady,\nP3,Widget 3,3,,Z-3\n")
    parsed = output([{"manufacturer_1": "Acme", "manufacturer_item_number_1": "A-1"},
                     {"manufacturer_1": "Brady"}, {"manufacturer_item_number_1": "Z-3"}])
    _, _, service = validate(source, parsed)
    assert service.validate().status == "awaiting_approval"


def test_pair_count_ignores_documents_and_lone_halves():
    rows = output([{"manufacturer_1": "Brady"}, {"manufacturer_item_number_1": "Z-3"}])
    assert _manufacturer_pairs(rows) == (2, 0)
    document = {column: None for column in CANONICAL_COLUMNS}
    document.update(line_number=1, item_number="DOC-1", item_name="Drawing", item_category="Document", quantity=1)
    stream = io.BytesIO()
    pd.DataFrame([document], columns=CANONICAL_COLUMNS).to_excel(stream, index=False)
    assert _manufacturer_pairs(stream.getvalue()) == (0, 0)


def test_malformed_row_map_feedback_names_the_expected_shape():
    message = _row_feedback({"valid": False, "error": "ROW_MAP_ROWS_INVALID"})
    assert "flat list of distinct integer row numbers" in message and "never [sheet, row] pairs" in message
    assert _row_feedback({"valid": False, "error": "ROW_MAP_UNVERIFIABLE"}).startswith("row_map_invalid:ROW_MAP_UNVERIFIABLE; write")


def test_authoring_rules_keep_lone_halves_and_read_columns_like_a_person():
    rules = " ".join(CANONICAL_OUTPUT_RULES)
    assert "both be populated or both be empty" not in rules
    assert "keeps that value in its pair and leaves the other half empty" in rules
    assert "header words are clues, not rules" in rules
    assert "never [sheet, row] pairs" in ROW_MAP_RULE
