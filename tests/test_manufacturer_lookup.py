"""One more parser attempt when makers and MPNs sit on another sheet, and held files say why (A047, Nick 2026-10-14).

QuickQuote A047 (a copy of A045): the combined legacy .xls passed everything except the manufacturer
check. Its lines are on sheet BOM; makers and MPNs for two of the parts are on sheet MFG keyed by part
number, and the parser left them out, so the file was held for a person straight away. Now the stop
records the MFG sheet from the source, allows ONE automatic refinement that brings the values in by
lookup, and holds as before when that retry still has no pair. A package review (which reads status
without automation) reports a held file's reason without starting anything. Synthetic data only;
no Azure/model calls. Run: pytest tests/test_manufacturer_lookup.py
"""
import io
import json

import pytest
from openpyxl import Workbook

from bom_processor import (CandidateAutomationService, CandidateStatusService, CandidateValidationError,
                           CandidateValidationService, InMemoryStorage, ParentPackageService, ProcessingRequest)
from bom_processor import bom_package_choice
from bom_processor.candidate_authoring import CandidateAuthoringService
from bom_processor.core import CANONICAL_COLUMNS, _json_bytes
from bom_processor.manufacturer_lookup import claim_retry, lookup_hint, retry_hint, retry_state
from legacy_xls import legacy_xls
from test_combined_bom_legacy_xls import A1, A2, BOM, MFG, PARSER, REQUESTED, RFQ, LocalWorker

REQUEST = "a047-shape"
HELD = "manufacturer_data_review_required"
# The first attempt, like A047's: makers and MPNs left out, the MFG rows skipped as another sheet.
FIRST = PARSER.replace("enumerate(makers.get(part, []), 1)", "enumerate([], 1)").replace(
    "skipped = [", 'skipped = [{"sheet": "MFG", "rows": [[2, mfg.nrows]], "reason": "other_sheet", '
    '"rationale": "Maker sheet."}] + [')
assert FIRST != PARSER


class Model:
    """Returns the given parsers in turn and keeps each prompt."""
    def __init__(self, *codes):
        self.codes, self.prompts = list(codes), []

    def generate(self, prompt, source_bytes, prior_code=None, failed_rule_id=None):
        self.prompts.append(prompt)
        return self.codes.pop(0)


class Starter:
    def __init__(self):
        self.calls = []

    def start(self, values):
        self.calls.append(dict(values))
        return f"execution-{len(self.calls)}"


def source_store(mfg=MFG):
    store = InMemoryStorage()
    source = legacy_xls({"BOM": BOM, "MFG": mfg})
    path = f"customer/rfq-{RFQ}/package-1/original-boms/attachment-1.xls"
    store.sources[path] = source
    import hashlib
    documents = [{"document_id": "doc-1", "blob_path": path, "sha256": hashlib.sha256(source).hexdigest(),
                  "size_bytes": len(source)}]
    store.registry[f"candidates/requests/{REQUEST}.json"] = _json_bytes({
        "version": "candidate-request.v2", "request_id": REQUEST, "status": "requested", "parser_key": "Parser1",
        "customer_key": "customer", "profile_hash": "a" * 64, "profile": {"extension": ".xls"},
        "source_evidence": documents})
    return store, source, documents


def failed_validation(store, candidate_id):
    with pytest.raises(CandidateValidationError, match="SEMANTIC_INVALID"):
        CandidateValidationService(REQUEST, candidate_id, store, LocalWorker(store)).validate()
    path = next(path for path in store.candidates if f"events/{REQUEST}/{candidate_id}/validation-failed-" in path)
    return json.loads(store.candidates[path])


def output_without_pairs(paired=()):
    book = Workbook()
    sheet = book.active
    sheet.append(list(CANONICAL_COLUMNS))
    for number, (_, _, part, name, quantity, _) in enumerate(BOM[1:], 1):
        row = dict.fromkeys(CANONICAL_COLUMNS)
        drawing = "ARTWORK" in name or "FABRICATION" in name
        row.update(line_number=number, item_number=part, item_name=name, quantity=quantity,
                   item_category="Document" if drawing else "Part")
        if part in paired:
            row.update(manufacturer_1="KEMET", manufacturer_item_number_1="X-" + part)
        sheet.append([row[column] for column in CANONICAL_COLUMNS])
    stream = io.BytesIO()
    book.save(stream)
    return stream.getvalue()


def test_the_maker_sheet_is_found_from_the_source_only_for_lines_without_a_pair():
    source = legacy_xls({"BOM": BOM, "MFG": MFG})
    hint = lookup_hint(source, ".xls", output_without_pairs(), {"BOM"})
    assert hint == {"version": "manufacturer-lookup.v1", "sheets": [{
        "sheet": "MFG", "header_row": 1, "key_column": 2, "manufacturer_columns": [5], "mpn_columns": [4], "parts": 2}]}
    # The lines' own sheet is never a lookup sheet; paired parts need nothing; no maker headings, no retry.
    assert lookup_hint(source, ".xls", output_without_pairs(), {"BOM", "MFG"}) is None
    assert lookup_hint(source, ".xls", output_without_pairs(paired=("0630-03236", "0630-03323")), {"BOM"}) is None
    plain = legacy_xls({"BOM": BOM, "MFG": [["Original", "Part Number", "Description", "Note", "Remark"]] + MFG[1:]})
    assert lookup_hint(plain, ".xls", output_without_pairs(), {"BOM"}) is None


def test_only_a_well_formed_manufacturer_stop_may_be_retried_and_only_once_per_request():
    hint = {"version": "manufacturer-lookup.v1", "sheets": [{"sheet": "MFG", "header_row": 1, "key_column": 2,
                                                             "manufacturer_columns": [5], "mpn_columns": [4], "parts": 2}]}
    assert retry_hint({"review_reason": HELD, "manufacturer_lookup": hint}) == hint
    assert retry_hint({"review_reason": "other", "manufacturer_lookup": hint}) is None
    assert retry_hint({"review_reason": HELD, "manufacturer_lookup": {**hint, "extra": 1}}) is None
    assert retry_hint({"review_reason": HELD}) is None
    store = InMemoryStorage()
    assert retry_state(store, "req", "c0") == "eligible"
    assert claim_retry(store, "req", "c0", "v1") is True
    assert retry_state(store, "req", "c0") == "dispatched" and retry_state(store, "req", "c1") == "spent"
    assert claim_retry(store, "req", "c1", "v2") is False
    other = InMemoryStorage()
    other.registry["candidate-automation/req/manufacturer-lookup-retry.json"] = b"not json"
    assert retry_state(other, "req", "c0") == "spent"  # fail closed


def test_the_retry_brings_the_other_sheets_values_in_and_validation_passes():
    store, _, _ = source_store()
    model = Model(FIRST, PARSER)
    first = CandidateAuthoringService(REQUEST, store, model).author()
    event = failed_validation(store, first.candidate_id)
    assert event["review_reason"] == HELD and event["repairable"] is False
    assert event["manufacturer_lookup"]["sheets"][0]["sheet"] == "MFG"

    starter = Starter()
    automation = CandidateAutomationService(store, author_starter=starter)
    status = CandidateStatusService(store, automation=automation).get(REQUEST)
    assert "automation_error" not in status and len(starter.calls) == 1  # in progress, not held
    values = starter.calls[0]
    assert values["AUTHOR_REFINE_CANDIDATE"] == first.candidate_id
    # Polling again while the retry runs starts nothing more.
    CandidateStatusService(store, automation=automation).get(REQUEST)
    assert len(starter.calls) == 1

    # The author job, as candidate_job.py runs it.
    retried = CandidateAuthoringService(REQUEST, store, model, feedback=values.get("AUTHOR_FEEDBACK"),
                                        refine=(values["AUTHOR_REFINE_CANDIDATE"], values["AUTHOR_REFINE_VALIDATION"])).author()
    instructions = [rule for rule in model.prompts[1].rules if rule.startswith("MANUFACTURER LOOKUP REFINEMENT")]
    assert len(instructions) == 1 and '"sheet":"MFG"' in instructions[0]
    assert model.prompts[1].structural_profile["previous_parser_code"] == FIRST
    validated = CandidateValidationService(REQUEST, retried.candidate_id, store, LocalWorker(store)).validate()
    assert validated.status == "awaiting_approval"


def test_a_retry_that_still_has_no_pair_is_held_for_a_person():
    store, _, _ = source_store()
    again = FIRST + "# second try\n"
    model = Model(FIRST, again)
    first = CandidateAuthoringService(REQUEST, store, model).author()
    failed_validation(store, first.candidate_id)
    starter = Starter()
    automation = CandidateAutomationService(store, author_starter=starter)
    CandidateStatusService(store, automation=automation).get(REQUEST)
    values = starter.calls[0]
    retried = CandidateAuthoringService(REQUEST, store, model, refine=(values["AUTHOR_REFINE_CANDIDATE"],
                                                                        values["AUTHOR_REFINE_VALIDATION"])).author()
    assert failed_validation(store, retried.candidate_id)["review_reason"] == HELD
    status = CandidateStatusService(store, automation=automation).get(REQUEST)
    assert status["candidate_id"] == retried.candidate_id and status["automation_error"] == HELD
    assert len(starter.calls) == 1


def test_a_review_reports_a_held_file_without_starting_anything(monkeypatch):
    store, _, documents = source_store()
    first = CandidateAuthoringService(REQUEST, store, Model(FIRST)).author()
    failed_validation(store, first.candidate_id)

    def refuse(*args, **kwargs):
        raise AssertionError("the review path must never start a job")
    monkeypatch.setattr(CandidateAutomationService, "start_author_refinement", refuse)
    monkeypatch.setattr(CandidateAutomationService, "start_manufacturer_retry", refuse)

    # While the one retry is still to run, status without automation shows it in progress.
    assert "automation_error" not in CandidateStatusService(store).get(REQUEST)
    # Once the request's retry is spent (here claimed by another candidate), the file is held and says so.
    claim_retry(store, REQUEST, "an-earlier-candidate", "v0")
    assert CandidateStatusService(store).get(REQUEST)["automation_error"] == HELD

    packages = ParentPackageService(store)
    plan = packages.plan(ProcessingRequest.from_dict({"job_id": "a047-review", "customer_key": "customer",
        "parser_key": "Parser1", "rfq_uuid": RFQ, "package_version": "1", "documents": documents}),
        approved_assignments=[], pending_assignments=[{"request_id": REQUEST, "document_ids": ["doc-1"]}])
    path = "10055369-002_1.zip/BOM/BOM_10055369-002_1.xls"
    choice = {"version": bom_package_choice.RESULT_VERSION_V2, "request_id": "combined-choice", "model_calls": 0,
              "requested_assemblies": REQUESTED, "findings": [], "choices": [], "primary_selections": [],
              "survey": {"files": [{"file_id": "file-1", "path": path, "kind": "spreadsheet", "role": "bom",
                                    "requested_assembly": None}], "findings": [], "has_blocking_findings": False},
              "documents": [{"document_id": "doc-1", "sha256": documents[0]["sha256"], "decision": "process",
                             "reason": "combined_bom", "disposition": None, "survey_file_ids": ["file-1"],
                             "roles": ["bom"], "assemblies": ["0100-04931", "0100-04932"],
                             "requested_assembly_ids": [A1, A2]}]}
    original = packages._plan
    state = {"request_id": "combined-choice", "status": "completed", "result_sha256": "c" * 64, "result": choice}
    monkeypatch.setattr(packages, "_plan", lambda plan_id: {**original(plan_id),
                                                            "package_choice": bom_package_choice.choice_reference(state)})
    monkeypatch.setattr(bom_package_choice, "bound_state", lambda storage, reference: state)
    group = packages.review(plan["plan_id"])["groups"][0]
    assert group["status"] == "pending" and group["automation_error"] == HELD
    assert group["source_file_paths"] == [path]
    assert group["source_assemblies"] == ["0100-04931", "0100-04932"]
