"""Step 6 regressions: immutable six-try lineage and partial package failures.

Inputs: synthetic workbooks, in-memory storage and scripted callers. Outputs:
assertions only. Run: py -3.12 -m pytest tests/test_package_budget_reopen_failures.py.
"""
import copy
import json

import pytest

from bom_processor.author_budget import Budget, BudgetError, enroll
from bom_processor.author_policy import resolve
from bom_processor.bom_package_choice import PackageChoiceService, choice_request, decide
from bom_processor.candidates import AggregateCandidateRequiredError
from bom_processor.package_resolution import ParentPackageService, PackageResolutionError
from bom_processor.package_survey import SurveyBatch, build_view, expand_package, plan_batches
from test_author_budget import NOW, finish
from test_bom_package_choice import (Agent, DOCS, FILES, PACKAGE, RFQ, Starter, archive,
                                    completed_service, decisions, lines, make_request,
                                    remote, reopen_request, store, xlsx, HEADER)


def pending(processor, request):
    with pytest.raises(AggregateCandidateRequiredError) as required:
        processor.process(request)
    assert len(required.value.candidates) == 1
    return required.value.candidates[0].request_id, required.value.plan_id


def reopened_group(mode=True):
    storage, service, request, _ = completed_service()
    processor = remote(storage, service)
    first, first_plan = pending(processor, request)
    assert Budget(storage, first, clock=lambda: NOW).inspect()["status"] == "available"
    resolve(storage, first, mode)
    claim = Budget(storage, first, clock=lambda: NOW).reserve("author")
    assert claim["slot"] == 1
    finish(storage, claim)
    before = copy.deepcopy(storage.registry)
    second, second_plan = pending(processor, reopen_request(["doc-plain"]))
    return storage, processor, first, second, first_plan, second_plan, before


@pytest.mark.parametrize("mode", [False, True])
def test_reopen_carries_used_try_and_pinned_mode_without_rewriting_history(mode):
    storage, processor, first, second, first_plan, second_plan, before = reopened_group(mode)
    assert first != second and first_plan != second_plan
    assert Budget(storage, second, clock=lambda: NOW).inspect()["used"] == 1
    assert resolve(storage, second, not mode)["source_associations"] is mode
    assert Budget(storage, first, clock=lambda: NOW).inspect()["group_id"] == Budget(storage, second, clock=lambda: NOW).inspect()["group_id"]
    for key, value in before.items():
        assert storage.registry[key] == value, key
    # Repeated parent processing is idempotent and consumes no extra slots.
    assert pending(processor, reopen_request(["doc-plain"])) == (second, second_plan)
    assert Budget(storage, second, clock=lambda: NOW).inspect()["used"] == 1


def test_seventh_try_after_reopen_is_held_for_both_requests():
    storage, _, first, second, _, second_plan, _ = reopened_group()
    budget = Budget(storage, second, clock=lambda: NOW)
    resolve(storage, second, False)
    for slot in range(2, 7):
        claim = budget.reserve("author")
        assert claim["status"] == "reserved" and claim["slot"] == slot
        finish(storage, claim)
    assert budget.reserve("author")["reason"] == "budget_exhausted"
    assert Budget(storage, first, clock=lambda: NOW).reserve("author")["reason"] == "budget_exhausted"
    assert budget.inspect()["used"] == 6
    review = ParentPackageService(storage).review(second_plan)
    held = next(item for item in review["groups"] if item.get("request_id") == second)
    assert held["automation_error"] == "budget_exhausted"
    assert held["source_assemblies"] == ["ASSY-100", "ASSY-200"]
    assert len(held["source_file_paths"]) == 3
    assert any(path.endswith("ASSY-100 BOM.xlsx") for path in held["source_file_paths"])


def test_reopen_extension_rechecks_stored_plan_and_result():
    storage, _, first, second, _, second_plan, _ = reopened_group()
    path = f"packages/{second_plan}/package-plan.v1.json"
    saved = storage.registry[path]
    value = json.loads(saved)
    value["sources"][0]["sha256"] = "0" * 64
    storage.registry[path] = json.dumps(value).encode()
    assert Budget(storage, second, clock=lambda: NOW).inspect()["reason"] == "budget_invalid"
    storage.registry[path] = saved
    member = json.loads(storage.registry[f"author-budget/v1/members/{second}.json"])
    policy = json.loads(storage.registry[f"author-budget/v1/groups/{member['group_id']}/policy.json"])
    choice_path = f"package-choice/{policy['package_choice']['request_id']}/result.json"
    value = json.loads(storage.registry[choice_path])
    value["model_calls"] += 1
    storage.registry[choice_path] = json.dumps(value).encode()
    assert Budget(storage, second, clock=lambda: NOW).inspect()["reason"] == "budget_invalid"


def test_added_file_without_verified_reopen_is_refused():
    from test_author_budget import Storage, plan, ready
    storage, _, _ = ready()
    expanded = plan(storage, [("request-new", ["doc-1", "doc-2", "doc-3"], "a" * 64)])
    with pytest.raises(BudgetError):
        enroll(storage, expanded, "request-new")


def test_one_assembly_choice_failure_keeps_its_boms_and_other_choice_succeeds():
    b_plain = xlsx({"BOM": [["Assembly", "ASSY-200"], HEADER] + lines(3, start=50)})
    b_mod = xlsx({"BOM": [["Assembly", "ASSY-200"], HEADER] + lines(4, start=50)})
    files = {**FILES, "B/ASSY-200 BOM.xlsx": b_plain, "B/ASSY-200 MOD BOM.xlsx": b_mod}
    docs = {**DOCS, "doc-other": b_plain, "doc-bmod": b_mod}
    package = archive(files)
    a = Agent()
    b = Agent(labels={"ASSY-200 BOM.xlsx": "redundant"}, primary_name="ASSY-200 MOD BOM.xlsx")
    calls = []

    def call(instructions, text):
        body, _ = json.JSONDecoder().raw_decode(text)
        calls.append(body.get("assembly", "survey"))
        if "classify" in body:
            return a(instructions, text)
        if body["assembly"] == "ASSY-100":
            raise RuntimeError("transport detail must not be retained")
        return b(instructions, text)

    storage = store(docs, package)
    service = PackageChoiceService(storage, Starter())
    request = make_request(docs, package)
    started = service.ensure(request)
    done = service.run(started["request_id"], call)
    assert done["status"] == "completed"
    assert decisions(done)["doc-mod"] == ("process", "choice_failed")
    assert decisions(done)["doc-plain"] == ("process", "choice_failed")
    assert decisions(done)["doc-other"] == ("excluded", "redundant")
    assert decisions(done)["doc-bmod"] == ("process", "primary")
    assert done["result"]["model_calls"] == 4
    finding = done["result"]["findings"][0]
    assert finding["code"] == "CHOICE_ASSEMBLY_FAILED" and finding["assemblies"] == ["ASSY-100"]
    assert finding["document_ids"] == ["doc-mod", "doc-plain"]
    assert len(finding["file_paths"]) == 2
    assert "transport detail" not in json.dumps(done)
    with pytest.raises(AggregateCandidateRequiredError) as required:
        remote(storage, service).process(request)
    review = ParentPackageService(storage).review(required.value.plan_id)
    assert review["package_choice_findings"] == [finding]
    assert sorted(doc for candidate in required.value.candidates for doc in candidate.document_ids) == ["doc-bmod", "doc-mod", "doc-plain"]


@pytest.mark.parametrize("transport", [False, True])
def test_an_incomplete_survey_stops_for_a_person_before_any_parser_runs(transport, monkeypatch):
    # Round 7 (Nick 2026-10-11): an unclassified spreadsheet could be any assembly's BOM; the choice
    # stops (PACKAGE_SURVEY_INCOMPLETE) instead of parsing every BOM on an unknown basis.
    from bom_processor.bom_package_choice import PackageChoicePendingError
    from choice_job import _summary
    monkeypatch.setattr("bom_processor.package_survey_run.TRANSPORT_RETRY_DELAYS", (0.0, 0.0))
    calls = []

    def call(instructions, text):
        calls.append(text)
        assert "classify" in json.JSONDecoder().raw_decode(text)[0]
        if transport:
            raise RuntimeError("private exception detail")
        return {}

    storage, service, request, done = completed_service(call)
    # A call with no reply is retried twice before it uses up an attempt (QQ #182).
    assert done["status"] == "failed" and done["code"] == "PACKAGE_SURVEY_INCOMPLETE"
    assert len(calls) == (4 if transport else 2)
    # The survey stays with the failure for diagnosis; the log line carries codes and ids only.
    finding = next(item for item in done["survey"]["findings"] if item["code"] == "SURVEY_BATCH_FAILED")
    assert finding["severity"] == "blocking"
    assert all(item["classification_status"] == "unclassified" for item in done["survey"]["files"])
    line = _summary(done)
    assert line["code"] == "PACKAGE_SURVEY_INCOMPLETE" and line["survey_failures"][0]["code"] == "SURVEY_BATCH_FAILED"
    assert "private exception" not in json.dumps(done)
    # The BOM request gets the failed state; no plan and no parser request is created.
    with pytest.raises(PackageChoicePendingError) as stopped:
        remote(storage, service).process(request)
    assert (stopped.value.state, stopped.value.code) == ("failed", "PACKAGE_SURVEY_INCOMPLETE")
    assert not any(path.startswith("candidates/requests/") for path in storage.registry)


def test_failed_survey_batch_preserves_successful_batches(monkeypatch):
    from bom_processor.package_survey_run import survey_view
    view = build_view(expand_package([("package.zip", PACKAGE)]))
    original = json.loads(plan_batches(view)[0].user_text)
    ids = list(view.ids)
    batches = []
    for group in (ids[:2], ids[2:]):
        body = copy.deepcopy(original)
        body["classify"] = group
        body["files"] = [item for item in body["files"] if item["id"] in group]
        batches.append(SurveyBatch(tuple(group), json.dumps(body)))
    monkeypatch.setattr("bom_processor.package_survey_run.plan_batches", lambda _: batches)
    agent = Agent()

    def call(instructions, text):
        body, _ = json.JSONDecoder().raw_decode(text)
        return {} if body["classify"] == ids[:2] else agent(instructions, text)

    result = survey_view(view, call)
    assert result["model_calls"] == 3 and result["has_blocking_findings"]
    assert {item["file_id"] for item in result["files"] if item.get("classification_status") == "unclassified"} == set(ids[:2])
    assert all(item["evidence"] for item in result["files"] if item["file_id"] in ids[2:])


def test_partial_survey_cannot_be_system_accepted_even_when_parsers_are_ready(monkeypatch):
    from bom_processor.package_system_acceptance import SystemPackageService
    from test_bom_package_choice import chosen_package
    storage, _, plan, _ = chosen_package()
    service = SystemPackageService(storage)
    review = service.review(plan["plan_id"])
    review["package_choice_findings"] = [{"code": "SURVEY_BATCH_FAILED", "severity": "blocking"}]
    monkeypatch.setattr(service, "review", lambda *_: review)
    monkeypatch.setattr(service, "_verify_plan_sources", lambda *_: None)
    with pytest.raises(PackageResolutionError, match="SYSTEM_REVIEW_NOT_QUALIFIED"):
        service.accept_system(plan_id=plan["plan_id"], review_manifest_sha256=review["manifest_sha256"],
                       assembly_identities=[{"name": "Board", "revision": "A"}],
                       caller_application="synthetic-quickquote")
