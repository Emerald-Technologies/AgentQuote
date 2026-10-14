"""Operator-submitted parser candidates and their selection record (Nick 2026-10-14, option 1).

A package request enrolled in an author budget already has a model candidate. An operator submits
a hand-written parser through the normal authoring boundary and writes one immutable selection
record; status then chooses the operator candidate over the budget lineage, the normal validator
validates it, and the package review reads it, all without any approval being granted here.
Failure modes fail closed: two records, a tampered record, a record for a missing candidate, an
approved plan, and a dry run that must write nothing. Synthetic data only; no Azure or model calls.
Run: python -m pytest tests/test_operator_candidate.py
"""
import hashlib
import io
import json
from datetime import datetime, timezone

import pytest
from openpyxl import Workbook

from bom_processor import InMemoryStorage, ParentPackageService, ProcessingRequest
from bom_processor.author_budget import begin_execution, complete
from bom_processor.candidate_authoring import CandidateAuthoringService
from bom_processor.candidate_automation import CandidateAutomationService
from bom_processor.candidate_status import CandidateStatusError, CandidateStatusService
from bom_processor.candidate_validation import CandidateValidationService
from bom_processor.candidates import AggregateCandidateRequiredError
from bom_processor.fixed_layout_parser import build_parser
from bom_processor.operator_candidate import (FixedCodeAuthor, OfflineWorker, OperatorCandidateError, apply,
                                              selection_path, selection_record)
from bom_processor.remote import RemoteBomProcessor
from test_fixed_layout_parser import CLEAN, clean_rows

NOW = datetime(2026, 10, 14, tzinfo=timezone.utc)
RFQ = "12345678-1234-4234-8234-123456789012"
GOOD = build_parser(CLEAN)
# A model parser that writes nothing useful: the shape of a candidate the operator replaces.
BAD = GOOD.replace('"Part No"', '"Part Number Missing"')


def workbook(rows):
    book = Workbook()
    book.active.title = "BOM"
    for row in rows:
        book.active.append(row)
    stream = io.BytesIO()
    book.save(stream)
    return stream.getvalue()


def planned():
    """A two-BOM package of one layout, enrolled in the author budget, with a completed model candidate."""
    storage = InMemoryStorage()
    documents = []
    for index, pairs in enumerate((1, 1)):
        rows = clean_rows(pairs)
        rows[2][1] = f"CAP-{index}"
        # The assembly's own reference row (a Document) names each BOM's assembly for the review.
        rows.insert(1, [0, f"ASM-{index}", "01", "PCB ASSY, TEST", None, None, None, None, None, None])
        data = workbook(rows)
        path = f"cust/rfq-{RFQ}/package-1/original-boms/doc-{index}.xlsx"
        storage.sources[path] = data
        documents.append({"document_id": f"doc-{index}", "blob_path": path, "size_bytes": len(data),
                          "sha256": hashlib.sha256(data).hexdigest()})
    request = ProcessingRequest.from_dict({"job_id": "operator-package", "customer_key": "cust",
                                           "parser_key": "Parser1", "rfq_uuid": RFQ, "package_version": "1",
                                           "documents": documents})
    with pytest.raises(AggregateCandidateRequiredError) as error:
        RemoteBomProcessor(storage, None, author_budget_enabled=True).process(request)
    request_id = error.value.candidates[0].request_id

    class Starter:
        calls = []

        def start(self, values):
            self.calls.append(values)
            return "execution-1"

    starter = Starter()
    automation = CandidateAutomationService(storage, starter, author_budget_enabled=True, budget_clock=lambda: NOW)
    assert automation.start_author(request_id) == "started"
    model = CandidateAuthoringService(request_id, storage, FixedCodeAuthor(BAD)).author()
    values = starter.calls[-1]
    owner = begin_execution(storage, values["AUTHOR_BUDGET_GROUP"], int(values["AUTHOR_BUDGET_SLOT"]),
                            request_id, values["AUTHOR_BUDGET_RESERVATION"])
    complete(storage, values["AUTHOR_BUDGET_GROUP"], int(values["AUTHOR_BUDGET_SLOT"]), request_id,
             values["AUTHOR_BUDGET_RESERVATION"], "completed", model.candidate_id, execution_owner=owner)
    return storage, request_id, model.candidate_id, error.value.plan_id


def test_the_selected_operator_candidate_replaces_the_budget_lineage_and_is_validated_normally():
    storage, request_id, model_id, plan_id = planned()
    assert CandidateStatusService(storage).get(request_id)["candidate_id"] == model_id
    authored, record = apply(storage, request_id, GOOD, actor="operator-1", reason="hand parser for this layout",
                             clock=lambda: NOW)
    assert storage.registry[selection_path(request_id, authored.candidate_id)] == json.dumps(
        record, sort_keys=True, separators=(",", ":")).encode()
    status = CandidateStatusService(storage).get(request_id)
    assert (status["candidate_id"], status["status"]) == (authored.candidate_id, "generated_unvalidated")
    # The normal validator, unchanged, takes it to awaiting a person's approval.
    result = CandidateValidationService(request_id, authored.candidate_id, storage, OfflineWorker(storage)).validate()
    assert result.status == "awaiting_approval"
    status = CandidateStatusService(storage).get(request_id)
    assert (status["candidate_id"], status["status"]) == (authored.candidate_id, "awaiting_approval")
    # The package review reads the same status: the operator candidate, still awaiting approval.
    review = ParentPackageService(storage).review(plan_id)
    children = [child for group in review["groups"] for child in group["evidence"]["children"]]
    assert len(review["groups"]) == 2 and {group["status"] for group in review["groups"]} == {"ready"}
    assert {(child["request_id"], child["candidate_id"]) for child in children} == {
        (request_id, authored.candidate_id)}
    assert review["approval_ready"] is True  # ready for a person's approval, not approved
    assert not any(path.startswith("approved/") or path.startswith("registry/") for path in storage.registry)


def test_status_starts_the_normal_validator_for_the_selected_candidate_and_never_reauthors_it():
    storage, request_id, _, _ = planned()
    # Reads quantity from the wrong column: parts get no quantity, so validation refuses the output.
    columns = {key: value for key, value in CLEAN["columns"].items() if key != "revision"}
    wrong = build_parser({**CLEAN, "columns": {**columns, "quantity": "Rev"}})
    authored, _ = apply(storage, request_id, wrong, actor="operator-1", reason="try", clock=lambda: NOW)

    class Recorder:
        def __init__(self):
            self.calls = []

        def start(self, values):
            self.calls.append(values)
            return "execution-v"

    validator, author = Recorder(), Recorder()
    automation = CandidateAutomationService(storage, author, validator, author_budget_enabled=True,
                                            budget_clock=lambda: NOW)
    CandidateStatusService(storage, automation=automation).get(request_id)
    assert [call["CANDIDATE_ID"] for call in validator.calls] == [authored.candidate_id]
    # The operator parser fails validation: a person fixes it; the model is not asked to refine it.
    from bom_processor.candidate_validation import CandidateValidationError
    try:
        # The validator job the status call started, run here with the slot it was given.
        outcome = CandidateValidationService(
            request_id, authored.candidate_id, storage, OfflineWorker(storage),
            serial_owner=validator.calls[0]["BOM_VALIDATOR_SERIAL_OWNER"]).validate().status
    except CandidateValidationError as exc:
        outcome = exc.code
    assert outcome != "awaiting_approval"
    status = CandidateStatusService(storage, automation=automation).get(request_id)
    assert status["status"] == "schema_invalid"
    assert status["automation_error"] == "validation_failure_requires_review"
    assert author.calls == []


def test_two_selection_records_fail_closed():
    storage, request_id, model_id, _ = planned()
    authored, _ = apply(storage, request_id, GOOD, actor="operator-1", reason="first", clock=lambda: NOW)
    manifest = json.loads(storage.candidates[f"candidates/{request_id}/{model_id}/candidate-manifest.json"])
    second = selection_record(request_id=request_id, candidate_id=model_id, code_sha256=manifest["code_sha256"],
                              actor="operator-2", reason="second", clock=lambda: NOW)
    storage.registry[selection_path(request_id, model_id)] = json.dumps(second).encode()
    with pytest.raises(CandidateStatusError):
        CandidateStatusService(storage).get(request_id)


@pytest.mark.parametrize("tamper", ["reason", "missing_candidate", "code_hash"])
def test_a_tampered_or_unbound_selection_record_fails_closed(tamper):
    storage, request_id, _, _ = planned()
    authored, record = apply(storage, request_id, GOOD, actor="operator-1", reason="first", clock=lambda: NOW)
    path = selection_path(request_id, authored.candidate_id)
    if tamper == "reason":
        storage.registry[path] = json.dumps({**record, "reason": "changed"}).encode()
    elif tamper == "missing_candidate":
        del storage.registry[path]
        other = selection_record(request_id=request_id, candidate_id="f" * 64, code_sha256=record["code_sha256"],
                                 actor="operator-1", reason="x", clock=lambda: NOW)
        storage.registry[selection_path(request_id, "f" * 64)] = json.dumps(other).encode()
    else:
        body = {key: value for key, value in record.items() if key != "selection_hash"}
        body["code_sha256"] = "0" * 64
        from bom_processor.operator_candidate import _hash
        storage.registry[path] = json.dumps({**body, "selection_hash": _hash(body)}).encode()
    with pytest.raises(CandidateStatusError):
        CandidateStatusService(storage).get(request_id)


def test_an_approved_plan_is_refused():
    storage, request_id, _, plan_id = planned()
    storage.registry[f"packages/{plan_id}/approvals/a.json"] = b"{}"
    with pytest.raises(OperatorCandidateError, match="OPERATOR_PLAN_ALREADY_APPROVED"):
        apply(storage, request_id, GOOD, actor="operator-1", reason="late", clock=lambda: NOW)


def test_a_policy_refused_parser_is_not_corrected_and_writes_nothing():
    storage, request_id, _, _ = planned()
    before = dict(storage.candidates), dict(storage.registry)
    with pytest.raises(Exception):
        apply(storage, request_id, GOOD.replace("import json", "import json\nimport os"),
              actor="operator-1", reason="bad", clock=lambda: NOW)
    assert not [path for path in storage.candidates if path not in before[0]]
    assert not [path for path in storage.registry if "operator-selections" in path]


def test_the_cli_dry_run_writes_nothing_and_lists_exactly_the_execute_writes():
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(
        "operator_cli", Path(__file__).resolve().parents[1] / "dev" / "operator_parser_candidate.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    storage, request_id, _, _ = planned()
    before = (dict(storage.candidates), dict(storage.registry))
    plan = cli.RecordingStorage(storage)
    authored, record = apply(plan, request_id, GOOD, actor="operator-1", reason="dry", hold_writer=False,
                             clock=lambda: NOW)
    assert (dict(storage.candidates), dict(storage.registry)) == before
    assert sorted(plan.writes) == sorted([
        f"candidates/{request_id}/{authored.candidate_id}/parse.py",
        f"candidates/{request_id}/{authored.candidate_id}/candidate-manifest.json",
        f"candidates/events/{request_id}/{authored.candidate_id}.json",
        selection_path(request_id, authored.candidate_id)])
    executed, _ = apply(storage, request_id, GOOD, actor="operator-1", reason="dry", clock=lambda: NOW)
    assert executed.candidate_id == authored.candidate_id
    for path, (data, _, _) in plan.writes.items():
        assert (storage.candidates.get(path) or storage.registry.get(path)) == data
