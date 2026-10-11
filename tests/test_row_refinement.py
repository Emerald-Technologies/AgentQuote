"""Failed-rows refinement (plan 8.8 step 4b): excerpt, prior code, 2 rounds then a person.
Synthetic data only."""
import hashlib
import io
import json

import pytest
from openpyxl import Workbook

from bom_processor import InMemoryStorage
from bom_processor.candidate_authoring import AUTHORING_VERSION, AuthoringError, CandidateAuthoringService
from bom_processor.candidate_automation import CandidateAutomationService
from bom_processor.candidates import persist_candidate
from bom_processor.core import _json_bytes
from bom_processor.master_registry import profile_bytes
from bom_processor.row_refinement import (HOLD_REASON, MAX_EXCERPT_ROWS, failed_row_excerpt,
                                          refinement_instructions, row_accounting_rounds)


def xlsx(rows):
    book = Workbook(); sheet = book.active; sheet.title = "BOM"
    for row in rows:
        sheet.append(row)
    data = io.BytesIO(); book.save(data); return data.getvalue()


SOURCE = xlsx([["Part", "Qty", "MFG", "MPN"], ["R-1", 2, "Acme", "A1"], [None, None, "Beta", "B1"],
               ["C-2", 1, "Gamma", "G1"]])
FAILURE = {"source_sha256": hashlib.sha256(SOURCE).hexdigest(), "error": None, "failed_row_count": 1,
           "failed_rows": [{"sheet": "BOM", "row": 3, "code": "ROW_UNACCOUNTED"}], "unmapped_columns": []}
PRIOR = ('import argparse\np = argparse.ArgumentParser()\np.add_argument("--input")\np.add_argument("--output")\n'
         'p.add_argument("--row-map")\n# prior parser\n')


def test_excerpt_carries_failed_rows_with_the_row_above_and_codes():
    excerpt = failed_row_excerpt(SOURCE, ".xlsx", FAILURE)
    assert [(item["row"], item["code"]) for item in excerpt["rows"]] == [(2, "context_row_above"), (3, "ROW_UNACCOUNTED")]
    assert excerpt["rows"][1]["cells"] == [[3, "Beta"], [4, "B1"]]
    assert excerpt["truncated"] is False


def test_excerpt_is_bounded_and_marks_truncation():
    rows = [["Part", "Qty"]] + [[f"P-{n}", n] for n in range(1, 400)]
    source = xlsx(rows)
    failure = {"failed_row_count": 300, "unmapped_columns": [],
               "failed_rows": [{"sheet": "BOM", "row": n, "code": "ROW_UNACCOUNTED"} for n in range(2, 302, 2)]}
    excerpt = failed_row_excerpt(source, ".xlsx", failure)
    assert len(excerpt["rows"]) <= MAX_EXCERPT_ROWS and excerpt["truncated"] is True


def test_instructions_name_the_round_and_forbid_dropping_rows():
    text = refinement_instructions(failed_row_excerpt(SOURCE, ".xlsx", FAILURE), 1, 2)
    assert text.startswith("ROW ACCOUNTING REFINEMENT round 1 of 2")
    assert "Never drop or invent a row" in text and "previous_parser_code" in text


class Starter:
    def __init__(self):
        self.calls = []

    def start(self, values):
        self.calls.append(dict(values))
        return f"execution-{len(self.calls)}"


def lineage(storage, request_id, chain):
    """Write refinement markers: chain[i+1] was refined from chain[i]."""
    root = f"candidate-automation/{request_id}/author-refinement/{AUTHORING_VERSION}"
    for attempt, (parent, child) in enumerate(zip(chain, chain[1:]), 1):
        storage.registry[f"{root}/attempt-{attempt}.started.json"] = _json_bytes({"refinement_of": parent})
        storage.registry[f"{root}/attempt-{attempt}.completed.json"] = _json_bytes({"candidate_id": child})


def test_rounds_follow_the_refinement_lineage():
    storage = InMemoryStorage()
    lineage(storage, "req", ["c0", "c1", "c2"])
    paths = lambda prefix: storage.list_registry_paths(prefix, 100)
    assert [row_accounting_rounds(storage, "req", c, AUTHORING_VERSION, paths) for c in ("c0", "c1", "c2")] == [0, 1, 2]


def event(candidate_id, validation_id="v1"):
    return {"event": "candidate.validation_failed", "failure": "SemanticError", "candidate_id": candidate_id,
            "validation_id": validation_id, "failure_message": "row_accounting_failed rows=1",
            "repairable": True, "row_accounting": FAILURE}


def test_first_row_accounting_failure_starts_a_fix_from_the_prior_candidate():
    storage, starter = InMemoryStorage(), Starter()
    automation = CandidateAutomationService(storage, author_starter=starter)
    assert automation.start_author_refinement("req", "c0", event("c0")) == "started"
    assert starter.calls[0]["AUTHOR_REFINE_CANDIDATE"] == "c0"
    assert starter.calls[0]["AUTHOR_REFINE_VALIDATION"] == "v1"


def test_after_two_failed_rounds_the_request_is_held_for_a_person():
    storage, starter = InMemoryStorage(), Starter()
    lineage(storage, "req", ["c0", "c1", "c2"])
    automation = CandidateAutomationService(storage, author_starter=starter)
    assert automation.start_author_refinement("req", "c2", event("c2")) == HOLD_REASON
    assert starter.calls == []


def test_other_failures_keep_the_existing_refinement_without_prior_code():
    storage, starter = InMemoryStorage(), Starter()
    lineage(storage, "req", ["c0", "c1", "c2"])
    automation = CandidateAutomationService(storage, author_starter=starter)
    plain = {"event": "candidate.validation_failed", "failure": "KeyError", "failure_message": "missing column"}
    automation.start_author_refinement("req", "c2", plain)
    assert all("AUTHOR_REFINE_CANDIDATE" not in call for call in starter.calls)


class Model:
    def __init__(self):
        self.prompts = []

    def generate(self, prompt, source_bytes):
        self.prompts.append(prompt)
        return PRIOR.replace("# prior parser", "# corrected parser")


def authored_failure():
    path = "customer/rfq-id/package-1/original-boms/doc.xlsx"
    storage = InMemoryStorage(sources={path: SOURCE})
    request = persist_candidate(storage, "Customer1", "customer-key", profile_bytes(SOURCE, ".xlsx"), ({
        "document_id": "doc1", "blob_path": path, "size_bytes": len(SOURCE),
        "sha256": hashlib.sha256(SOURCE).hexdigest()},))
    base = f"candidates/{request.request_id}/c0"
    storage.candidates[f"{base}/parse.py"] = PRIOR.encode()
    storage.candidates[f"{base}/candidate-manifest.json"] = _json_bytes({
        "candidate_id": "c0", "code_sha256": hashlib.sha256(PRIOR.encode()).hexdigest()})
    storage.candidates[f"candidates/events/{request.request_id}/c0/validation-failed-v1.json"] = _json_bytes(event("c0"))
    return storage, request


def test_refinement_prompt_has_prior_code_and_failed_rows_but_never_stores_them():
    storage, request = authored_failure()
    model = Model()
    CandidateAuthoringService(request.request_id, storage, model, refine=("c0", "v1")).author()
    prompt = model.prompts[0]
    assert prompt.structural_profile["previous_parser_code"] == PRIOR
    assert any(rule.startswith("ROW ACCOUNTING REFINEMENT round 1 of 2") and "Beta" in rule for rule in prompt.rules)
    assert "Beta" not in repr(prompt) and "prior parser" not in repr(prompt)
    stored = b"".join(data for path, data in storage.candidates.items() if "/c0/" not in path and "events/" not in path)
    assert b"Beta" not in stored and b"previous_parser_code" not in stored


@pytest.mark.parametrize("tamper", ["code", "event_candidate", "missing_event"])
def test_refinement_refuses_unbound_prior_code_or_failure(tamper):
    storage, request = authored_failure()
    base = f"candidates/{request.request_id}/c0"
    event_path = f"candidates/events/{request.request_id}/c0/validation-failed-v1.json"
    if tamper == "code":
        storage.candidates[f"{base}/parse.py"] += b"# changed"
    elif tamper == "event_candidate":
        storage.candidates[event_path] = _json_bytes(event("other"))
    else:
        del storage.candidates[event_path]
    with pytest.raises(AuthoringError, match="REFINEMENT_"):
        CandidateAuthoringService(request.request_id, storage, Model(), refine=("c0", "v1")).author()


def test_status_surfaces_the_hold_with_row_locations_only():
    import inspect
    from bom_processor import candidate_status
    source = inspect.getsource(candidate_status)
    assert 'outcome == "row_accounting_review_required"' in source
    assert 'result["row_accounting"] = failure_event["row_accounting"]' in source
    # The failure record holds counts, locations and codes, never cell values.
    assert set(FAILURE) <= {"source_sha256", "error", "failed_row_count", "failed_rows", "unmapped_columns",
                            "pattern_findings"}
    assert HOLD_REASON == "row_accounting_review_required"


def test_excerpt_and_instructions_show_the_cited_field_and_column():
    failure = {**FAILURE, "failed_rows": [{"sheet": "BOM", "row": 2, "code": "FIELD_VALUE_MISMATCH"}]}
    fields = [{"sheet": "BOM", "row": 2, "field": "manufacturer_1", "column": 1, "part": True}]
    excerpt = failed_row_excerpt(SOURCE, ".xlsx", failure, fields)
    row = next(item for item in excerpt["rows"] if item["row"] == 2)
    assert row["cited"] == [{"field": "manufacturer_1", "column": 1, "part": True}]
    text = refinement_instructions(excerpt, 1, 2)
    assert "cited lists each output field" in text and '{"derived": reason}' in text
    # Without field detail the excerpt is unchanged (older failure events).
    assert "cited" not in next(item for item in failed_row_excerpt(SOURCE, ".xlsx", failure)["rows"] if item["row"] == 2)


def test_row_feedback_names_cited_cells_and_the_rule_explains_derived_values():
    from bom_processor.candidate_authoring import ROW_MAP_RULE
    from bom_processor.candidate_validation import _row_feedback
    text = _row_feedback({"failed_row_count": 1, "failed_rows": [{"sheet": "BOM", "row": 5, "code": "FIELD_VALUE_MISMATCH"}],
                          "field_mismatches": [{"sheet": "BOM", "row": 5, "field": "bom_notes", "column": 17, "part": False}],
                          "unmapped_columns": [], "pattern_findings": []})
    assert "BOM!5 bom_notes<-col17" in text
    assert "Source category: Capacitor" in ROW_MAP_RULE and "several values separated by delimiters" in ROW_MAP_RULE
    assert "cited by no field goes in ignored_columns" in ROW_MAP_RULE
