"""Package choice job and wiring (plan 8.8 step 2): synthetic, offline, stub agent only."""
import copy
import hashlib
import json

import pytest

from bom_processor import InMemoryStorage, ProcessingError, ProcessingRequest, RemoteBomProcessor
from bom_processor.bom_candidate_choice import REPLY_VERSION as CHOICE_VERSION
from bom_processor.bom_package_choice import (PackageChoiceError, PackageChoicePendingError,
                                              PackageChoiceService, choice_request, decide)
from bom_processor.candidates import AggregateCandidateRequiredError, CandidateRequest
from bom_processor.master_registry import StructuralProfile
from bom_processor.package_resolution import ParentPackageService
from bom_processor.package_survey import build_view, expand_package
from test_package_survey import archive, xlsx

RFQ = "12345678-1234-4234-8234-123456789abc"
ZIP_ID = "2222abcd-3333-4444-8555-666666666666"
HEADER = ["Item", "Part Number", "Manufacturer", "MPN", "Qty"]


def lines(count, start=1):
    return [[index, f"P-{index}", "Maker", f"M-{index}", 1] for index in range(start, start + count)]


MOD = xlsx({"BOM": [["Assembly", "ASSY-100"], HEADER] + lines(6)})
PLAIN = xlsx({"BOM": [["Assembly", "ASSY-100"], HEADER] + lines(5)})
OTHER = xlsx({"BOM": [["Assembly", "ASSY-200"], HEADER] + lines(4, start=50)})
MPI = xlsx({"MPI": [["Manufacturing Process Instruction"], ["Assembly:ASSY-200"], ["Step", "Operation"], [10, "Paste"]]})
FILES = {"A/ASSY-100 MOD BOM.xlsx": MOD, "A/ASSY-100 BOM.xlsx": PLAIN,
         "B/ASSY-200 BOM.xlsx": OTHER, "B/ASSY-200 MPI.xlsx": MPI}
PACKAGE = archive(FILES)
DOCS = {"doc-mod": MOD, "doc-plain": PLAIN, "doc-other": OTHER, "doc-mpi": MPI}


def source_path(doc):
    return f"cust/rfq-{RFQ}/package-1/original-boms/{doc}.xlsx"


def package_path(data):
    return f"evidence-inputs/{RFQ}/package-1/{ZIP_ID}/{hashlib.sha256(data).hexdigest()}"


def make_request(docs=DOCS, package=PACKAGE, with_files=True):
    value = {"job_id": "job-1", "customer_key": "cust", "parser_key": "Parser1", "rfq_uuid": RFQ,
             "package_version": "1",
             "documents": [{"document_id": doc, "blob_path": source_path(doc), "size_bytes": len(data),
                            "sha256": hashlib.sha256(data).hexdigest()} for doc, data in sorted(docs.items())]}
    if with_files:
        value["package_files"] = [{"document_id": ZIP_ID, "file_name": "package.zip",
                                   "blob_path": package_path(package), "size_bytes": len(package),
                                   "sha256": hashlib.sha256(package).hexdigest()}]
    return ProcessingRequest.from_dict(value)


def store(docs=DOCS, package=PACKAGE):
    sources = {source_path(doc): data for doc, data in docs.items()}
    sources[package_path(package)] = package
    return InMemoryStorage(sources=sources)


def cell(sheet, row, column, value):
    return {"source": "cell", "value": value, "cell": {"sheet": sheet, "row": row, "column": column}}


class Agent:
    """Stub agent: survey replies from file names; choice reply from a fixed label table."""

    def __init__(self, labels=None, primary_name="ASSY-100 MOD BOM.xlsx", wrong_columns=False):
        self.labels = labels or {"ASSY-100 BOM.xlsx": "redundant"}
        self.primary_name, self.wrong_columns, self.calls = primary_name, wrong_columns, []

    def __call__(self, instructions, text):
        self.calls.append(instructions[:40])
        body, _ = json.JSONDecoder().raw_decode(text)
        if "classify" in body:
            files = []
            for entry in body["files"]:
                name = entry["path"].rsplit("/", 1)[-1]
                sheet = entry["sheets"][0]["name"]
                if "MPI" in name:
                    record = {"role": "work_instruction",
                              "assembly": {"name": [cell(sheet, 2, 1, "ASSY-200")], "revision": []},
                              "evidence": [cell(sheet, 1, 1, "Manufacturing Process Instruction")]}
                else:
                    assembly = "ASSY-100" if "100" in name else "ASSY-200"
                    record = {"role": "bom", "assembly": {"name": [cell(sheet, 1, 2, assembly)], "revision": []},
                              "evidence": [cell(sheet, 2, 2, "Part Number")]}
                files.append({"file_id": entry["id"], "possibly_shared": False,
                              "rationale": "Stub.", "findings": [], **record})
            return {"version": "package-survey-reply.v1", "files": files, "findings": []}
        files = []
        primary = None
        for entry in body["files"]:
            name = entry["path"].rsplit("/", 1)[-1]
            disposition = "primary" if name == self.primary_name else self.labels[name]
            if disposition == "primary":
                primary = entry["id"]
            columns = {"sheet": "BOM", "identity_column": 4 if self.wrong_columns else 2,
                       "quantity_column": 5, "first_row": 3, "last_row": None}
            files.append({"file_id": entry["id"], "disposition": disposition, "rationale": "Stub.",
                          "evidence": [cell("BOM", 2, 2, "Part Number")], "columns": columns})
        return {"version": CHOICE_VERSION, "primary": primary, "files": files,
                "rationale": "Stub choice.", "findings": []}


class Starter:
    def __init__(self):
        self.started = []

    def start(self, values):
        self.started.append(dict(values))
        return "execution-1"


def completed_service(agent=None, docs=DOCS):
    storage = store(docs)
    starter = Starter()
    service = PackageChoiceService(storage, starter)
    request = make_request(docs)
    state = service.ensure(request)
    assert state["status"] == "started" and starter.started == [{"REQUEST_ID": state["request_id"]}]
    done = service.run(state["request_id"], agent or Agent())
    return storage, service, request, done


def decisions(state):
    return {item["document_id"]: (item["decision"], item["reason"]) for item in state["result"]["documents"]}


def test_choice_sets_aside_the_redundant_bom_and_the_work_instruction():
    _, _, _, state = completed_service()
    assert state["status"] == "completed"
    assert decisions(state) == {"doc-mod": ("process", "primary"), "doc-plain": ("excluded", "redundant"),
                                "doc-other": ("process", "only_bom"), "doc-mpi": ("excluded", "work_instruction")}
    assert state["result"]["primary_selections"] == [
        {"assembly": "ASSY-100", "primary_document_ids": ["doc-mod"], "excluded_document_ids": ["doc-plain"]}]
    # One survey call plus one choice call for the only contested assembly.
    assert state["result"]["model_calls"] == 2


def test_a_choice_code_does_not_confirm_keeps_every_bom_in_processing():
    # The agent calls the larger BOM redundant: the column read disagrees, so nothing is set aside.
    agent = Agent(labels={"ASSY-100 MOD BOM.xlsx": "redundant"}, primary_name="ASSY-100 BOM.xlsx")
    _, _, _, state = completed_service(agent)
    assert decisions(state)["doc-mod"] == ("process", "choice_needs_review")
    assert decisions(state)["doc-plain"] == ("process", "choice_needs_review")
    assert state["result"]["primary_selections"] == []


def test_obsolete_label_cannot_be_confirmed_by_data_so_the_file_stays():
    _, _, _, state = completed_service(Agent(labels={"ASSY-100 BOM.xlsx": "obsolete"}))
    assert decisions(state)["doc-plain"] == ("process", "choice_needs_review")


def test_request_identity_and_api_side_never_calls_the_model():
    storage = store()
    starter = Starter()
    service = PackageChoiceService(storage, starter)
    first = service.ensure(make_request())
    again = service.ensure(make_request())
    assert first == again and len(starter.started) == 1
    assert first["request_id"] == choice_request(make_request())["request_id"]
    with pytest.raises(PackageChoiceError, match="PACKAGE_FILES_REQUIRED"):
        choice_request(make_request(with_files=False))


def test_worker_refuses_tampered_package_bytes_and_records_failure():
    storage = store()
    service = PackageChoiceService(storage, Starter())
    state = service.ensure(make_request())
    # Same size, different bytes.
    storage.sources[package_path(PACKAGE)] = PACKAGE[:-1] + bytes([PACKAGE[-1] ^ 1])
    result = service.run(state["request_id"], Agent())
    assert result == {"request_id": state["request_id"], "status": "failed", "code": "PACKAGE_FILE_IDENTITY_MISMATCH"}
    # A failed choice is final; a second run does not call the model.
    agent = Agent()
    assert service.run(state["request_id"], agent)["status"] == "failed" and agent.calls == []


def test_tampered_result_is_detected():
    storage, service, _, state = completed_service()
    path = f"package-choice/{state['request_id']}/result.json"
    value = json.loads(storage.registry[path])
    value["documents"][1]["decision"] = "process"
    storage.registry[path] = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    with pytest.raises(PackageChoiceError, match="PACKAGE_CHOICE_RESULT_CORRUPT"):
        service.status(state["request_id"])


def test_document_missing_from_the_package_is_processed_with_a_finding():
    extra = xlsx({"BOM": [HEADER] + lines(2, start=90)})
    docs = {**DOCS, "doc-extra": extra}
    _, _, _, state = completed_service(docs=docs)
    assert decisions(state)["doc-extra"] == ("process", "not_in_package")
    assert state["result"]["findings"][0]["code"] == "CHOICE_DOCUMENT_NOT_IN_PACKAGE"


def test_decide_refuses_a_package_with_no_bom_left():
    request = choice_request(make_request(docs={"doc-mpi": MPI}))
    with pytest.raises(PackageChoiceError, match="PACKAGE_NO_BOM"):
        decide(request, [("package.zip", PACKAGE)], Agent())


def remote(storage, service):
    class Orchestrator:
        def start(self, item):
            raise AssertionError("no parser runs before candidates exist")
    return RemoteBomProcessor(storage, Orchestrator(), package_choice=service, sleep=lambda _: None)


def test_remote_returns_pending_until_the_choice_completes_then_plans_with_excluded_groups():
    storage = store()
    service = PackageChoiceService(storage, Starter())
    processor = remote(storage, service)
    with pytest.raises(PackageChoicePendingError) as pending:
        processor.process(make_request())
    assert pending.value.state == "started"
    service.run(pending.value.request_id, Agent())
    with pytest.raises(AggregateCandidateRequiredError) as required:
        processor.process(make_request())
    # Only the two primary BOMs reach parser authoring; the set-aside files never do.
    authored = sorted(doc for item in required.value.candidates for doc in item.document_ids)
    assert authored == ["doc-mod", "doc-other"]
    for item in required.value.candidates:
        child = json.loads(storage.registry[f"candidates/requests/{item.request_id}.json"])
        assert child["package_choice"]["request_id"] == pending.value.request_id
    plan = json.loads(storage.registry[f"packages/{required.value.plan_id}/package-plan.v1.json"])
    excluded = [item for item in plan["assignments"] if item["kind"] == "excluded"]
    assert [(item["document_ids"], item["reason"]) for item in excluded] == [
        (["doc-mpi"], "work_instruction"), (["doc-plain"], "redundant")]
    assert plan["package_choice"]["request_id"] == pending.value.request_id
    review = ParentPackageService(storage).review(required.value.plan_id)
    groups = {tuple(group["document_ids"]): group["status"] for group in review["groups"]}
    assert groups[("doc-mpi",)] == "excluded" and groups[("doc-plain",)] == "excluded"
    assert groups[("doc-mod",)] == "pending" and not review["approval_ready"]


def reopen_request(reopen, docs=DOCS):
    value = json.loads(json.dumps({"job_id": "job-1", "customer_key": "cust", "parser_key": "Parser1",
                                   "rfq_uuid": RFQ, "package_version": "1"}))
    base = make_request(docs)
    value["documents"] = [{"document_id": d.document_id, "blob_path": d.blob_path, "size_bytes": d.size_bytes,
                           "sha256": d.sha256} for d in base.documents]
    value["package_files"] = [{"document_id": f.document_id, "file_name": f.file_name, "blob_path": f.blob_path,
                               "size_bytes": f.size_bytes, "sha256": f.sha256} for f in base.package_files]
    value["package_choice_reopen"] = reopen
    return ProcessingRequest.from_dict(value)


def test_reopen_request_field_is_validated_and_changes_the_fingerprint_only_when_present():
    assert make_request().package_choice_reopen is None
    reopened = reopen_request(["doc-plain"])
    assert reopened.fingerprint() != make_request().fingerprint()
    # The choice request is the same: reopening never runs the survey or choice again.
    assert choice_request(reopened)["request_id"] == choice_request(make_request())["request_id"]
    for bad in (["doc-x"], [], ["doc-plain", "doc-mod"], ["doc-plain", "doc-plain"], "doc-plain"):
        with pytest.raises(ProcessingError):
            reopen_request(bad)
    with pytest.raises(ProcessingError):
        ProcessingRequest.from_dict({**make_request(with_files=False).__dict__, "documents": [],
                                     "package_choice_reopen": ["doc-plain"]})


def test_reopen_brings_a_set_aside_bom_back_without_a_model_call_and_refuses_non_boms():
    from bom_processor.bom_package_choice import apply_reopen, bound_state, choice_reference
    storage, service, _, state = completed_service()
    reopened = apply_reopen(state, ["doc-plain"])
    assert decisions(reopened)["doc-plain"] == ("process", "reopened")
    assert decisions(reopened)["doc-mpi"] == ("excluded", "work_instruction")
    assert reopened["result"]["primary_selections"] == [
        {"assembly": "ASSY-100", "primary_document_ids": ["doc-mod", "doc-plain"], "excluded_document_ids": []}]
    # The stored result is unchanged and the binding re-applies the same reopen.
    assert service.status(state["request_id"]) == state
    reference = choice_reference(reopened)
    assert reference["reopened_document_ids"] == ["doc-plain"]
    assert bound_state(storage, reference) == reopened
    assert set(choice_reference(state)) == {"request_id", "result_sha256"}
    with pytest.raises(PackageChoiceError, match="PACKAGE_CHOICE_REOPEN_INVALID"):
        apply_reopen(state, ["doc-mpi"])  # the survey called it a work instruction
    with pytest.raises(PackageChoiceError, match="PACKAGE_CHOICE_REOPEN_INVALID"):
        apply_reopen(state, ["doc-mod"])  # already processed
    with pytest.raises(PackageChoiceError, match="PACKAGE_CHOICE_REOPEN_INVALID"):
        bound_state(storage, {**reference, "reopened_document_ids": ["doc-mpi"]})
    with pytest.raises(PackageChoiceError, match="PACKAGE_CHOICE_BINDING"):
        bound_state(storage, {**reference, "reopened_document_ids": ["doc-plain", "doc-plain"]})
    with pytest.raises(PackageChoiceError, match="PACKAGE_CHOICE_BINDING"):
        bound_state(storage, {**reference, "extra": 1})


def test_remote_with_reopen_processes_the_reopened_bom_under_a_new_plan():
    storage = store()
    service = PackageChoiceService(storage, Starter())
    processor = remote(storage, service)
    with pytest.raises(PackageChoicePendingError) as pending:
        processor.process(make_request())
    service.run(pending.value.request_id, Agent())
    with pytest.raises(AggregateCandidateRequiredError) as first:
        processor.process(make_request())
    with pytest.raises(AggregateCandidateRequiredError) as second:
        processor.process(reopen_request(["doc-plain"]))
    authored = sorted(doc for item in second.value.candidates for doc in item.document_ids)
    assert authored == ["doc-mod", "doc-other", "doc-plain"]
    assert second.value.plan_id != first.value.plan_id
    # New candidate request ids: the reopened binding is part of their identity.
    assert not {item.request_id for item in first.value.candidates} & {item.request_id for item in second.value.candidates}
    plan = json.loads(storage.registry[f"packages/{second.value.plan_id}/package-plan.v1.json"])
    assert plan["package_choice"]["reopened_document_ids"] == ["doc-plain"]
    assert [item["document_ids"] for item in plan["assignments"] if item["kind"] == "excluded"] == [["doc-mpi"]]
    review = ParentPackageService(storage).review(second.value.plan_id)
    assert {tuple(group["document_ids"]): group["status"] for group in review["groups"]}[("doc-mpi",)] == "excluded"
    # A reopen of a non-BOM fails closed.
    with pytest.raises(ProcessingError, match="package choice reopen invalid"):
        processor.process(reopen_request(["doc-mpi"]))


def test_remote_without_package_files_never_touches_the_choice_service():
    storage = store()

    class Refusing:
        def ensure(self, request):
            raise AssertionError("not called")

    with pytest.raises(AggregateCandidateRequiredError) as required:
        remote(storage, Refusing()).process(make_request(with_files=False))
    authored = sorted(doc for item in required.value.candidates for doc in item.document_ids)
    assert authored == sorted(DOCS)
    plan = json.loads(storage.registry[f"packages/{required.value.plan_id}/package-plan.v1.json"])
    assert "package_choice" not in plan and all(item["kind"] != "excluded" for item in plan["assignments"])


def test_remote_with_package_files_but_no_choice_job_fails_closed():
    storage = store()
    processor = RemoteBomProcessor(storage, object(), sleep=lambda _: None)
    with pytest.raises(ProcessingError, match="package choice unavailable"):
        processor.process(make_request())


def test_failed_choice_is_reported_as_pending_failed_with_its_code():
    storage = store()
    service = PackageChoiceService(storage, Starter())
    state = service.ensure(make_request())
    storage.sources[package_path(PACKAGE)] = PACKAGE + b"longer"
    service.run(state["request_id"], Agent())
    with pytest.raises(PackageChoicePendingError) as pending:
        remote(storage, service).process(make_request())
    assert (pending.value.state, pending.value.code) == ("failed", "PACKAGE_FILE_UNAVAILABLE")


def test_candidate_request_identity_is_unchanged_without_a_package_choice():
    class Profile:
        profile_hash = "a" * 64

        @staticmethod
        def to_dict():
            return {}

    evidence = ({"document_id": "doc-1", "blob_path": "p", "size_bytes": 1, "sha256": "b" * 64},)
    from bom_processor.master_registry import _hash
    expected = _hash({"version": "candidate-request.v2", "parser_key": "P", "customer_key": "c",
                      "profile_hash": "a" * 64, "source_evidence": list(evidence)})
    assert CandidateRequest.build("P", "c", Profile(), evidence, "t").request_id == expected
    bound = CandidateRequest.build("P", "c", Profile(), evidence, "t",
                                   package_choice={"request_id": "c" * 64, "result_sha256": "d" * 64})
    assert bound.request_id != expected and bound.to_dict()["package_choice"]["request_id"] == "c" * 64


def _choice_for(store_, request, excluded_doc, primary_doc, assembly="Board", set_aside_values=("R1",)):
    """Write a completed package choice directly (the decision logic is tested above).

    set_aside_values are the identity-column values of the set-aside file, stored as digests.
    """
    from bom_processor.bom_candidate_choice import value_digest
    from bom_processor.bom_package_choice import RESULT_VERSION, _hash, _json_bytes
    from bom_processor.semantic import _norm
    record = choice_request(request)
    rid = record["request_id"]
    file_ids = {d.document_id: f"F{i}" for i, d in enumerate(request.documents, 1)}
    documents = [{"document_id": d.document_id, "sha256": d.sha256, "survey_file_ids": [file_ids[d.document_id]],
                  "decision": "excluded" if d.document_id == excluded_doc else "process",
                  "reason": "redundant" if d.document_id == excluded_doc else "primary",
                  "disposition": "redundant" if d.document_id == excluded_doc else "primary",
                  "roles": ["bom"], "assemblies": [assembly]}
                 for d in request.documents]
    columns = {"sheet": "BOM", "identity_column": 2, "quantity_column": None, "first_row": 2, "last_row": None}
    choice = {"primary": file_ids[primary_doc], "automatic_selection_allowed": True,
              "files": [{"file_id": file_ids[primary_doc], "disposition": "primary", "columns": columns},
                        {"file_id": file_ids[excluded_doc], "disposition": "redundant", "columns": columns}]}
    choices = [{"assembly": assembly, "view_sha256": "e" * 64, "choice": choice,
                "set_aside_digests": {file_ids[excluded_doc]: sorted(value_digest(_norm(value))
                                                                    for value in set_aside_values)}}]
    result = {"version": RESULT_VERSION, "request_id": rid, "model_calls": 2, "survey": {}, "choices": choices,
              "documents": documents, "findings": [],
              "primary_selections": [{"assembly": assembly, "primary_document_ids": [primary_doc],
                                      "excluded_document_ids": [excluded_doc]}]}
    store_.registry[f"package-choice/{rid}/request.json"] = _json_bytes(record)
    store_.registry[f"package-choice/{rid}/result.json"] = _json_bytes(result)
    store_.registry[f"package-choice/{rid}/completed.json"] = _json_bytes(
        {"request_id": rid, "result_sha256": _hash(result)})
    return PackageChoiceService(store_).status(rid)


def chosen_package(set_aside_values=("R1",)):
    """Two equivalent BOMs; the choice keeps doc-1 and sets aside doc-2."""
    from test_package_resolution import package_workbook, request_and_store
    output = package_workbook(assembly=("Board", "A"))
    request, store_ = request_and_store(outputs=[output, output])
    package = archive({"a.csv": b"x"})
    value = {"job_id": request.job_id, "customer_key": request.customer_key, "parser_key": request.parser_key,
             "rfq_uuid": request.rfq_uuid, "package_version": request.package_version,
             "documents": [{"document_id": d.document_id, "blob_path": d.blob_path, "size_bytes": d.size_bytes,
                            "sha256": d.sha256} for d in request.documents],
             "package_files": [{"document_id": ZIP_ID, "file_name": "package.zip",
                                "blob_path": f"evidence-inputs/{request.rfq_uuid}/package-1/{ZIP_ID}/"
                                             f"{hashlib.sha256(package).hexdigest()}",
                                "size_bytes": len(package), "sha256": hashlib.sha256(package).hexdigest()}]}
    request = ProcessingRequest.from_dict(value)
    # The candidate child covers doc-1 only; doc-2 is set aside by the choice.
    child_path = "candidates/requests/request-1.json"
    child = json.loads(store_.registry[child_path])
    child["source_evidence"] = child["source_evidence"][:1]
    store_.registry[child_path] = json.dumps(child).encode()
    evidence_path = "candidates/request-1/candidate-1/validation/validation-1/evidence.json"
    evidence = json.loads(store_.candidates[evidence_path])
    evidence["fixtures"] = evidence["fixtures"][:1]
    evidence["fixture_count"] = 1
    store_.candidates[evidence_path] = json.dumps(evidence).encode()
    choice = _choice_for(store_, request, "doc-2", "doc-1", set_aside_values=set_aside_values)
    from bom_processor.package_system_acceptance import SystemPackageService
    service = SystemPackageService(store_)
    plan = service.plan(request, approved_assignments=[], package_choice=choice,
                        pending_assignments=[{"request_id": "request-1", "document_ids": ["doc-1"]}])
    return store_, service, plan, choice


def test_primary_selected_group_qualifies_for_system_acceptance_and_resolves_only_the_primary():
    _, service, plan, _ = chosen_package()
    review = service.review(plan["plan_id"])
    assert review["approval_ready"]
    by_kind = {group["kind"]: group for group in review["groups"]}
    assert by_kind["excluded"]["document_ids"] == ["doc-2"] and by_kind["excluded"]["status"] == "excluded"
    ready = by_kind["assembly"]
    assert ready["evidence"]["reconciliation"] == "primary_selected"
    assert ready["evidence"]["primary_selection"]["excluded_document_ids"] == ["doc-2"]
    post_parse = ready["evidence"]["post_parse"]
    assert post_parse["agreement"] == "agreed" and post_parse["review_reasons"] == []
    assert [(item["document_id"], item["parsed_field"], item["column_values_not_in_primary"])
            for item in post_parse["checks"]] == [("doc-2", "item_number", 0)]
    accepted = service.accept_system(plan_id=plan["plan_id"], review_manifest_sha256=review["manifest_sha256"],
                                     assembly_identities=[{"name": "Board", "revision": "A"}],
                                     caller_application="synthetic-quickquote")
    resolved = service.resolve(plan_id=plan["plan_id"], approval_id=accepted["approval_id"])
    assert [(item["document_ids"], item["reconciliation"]) for item in resolved["results"]] == [
        (["doc-1"], "primary_selected")]
    # Replay of the published manifest validates with the excluded document left out.
    assert service.manifest(plan["plan_id"], accepted["approval_id"]) == resolved


def test_post_parse_contradiction_is_recorded_and_system_acceptance_refuses_it():
    from bom_processor.package_resolution import PackageResolutionError
    # The set-aside file holds R2, which the parsed primary lacks: "redundant" is contradicted.
    _, service, plan, _ = chosen_package(set_aside_values=("R1", "R2"))
    review = service.review(plan["plan_id"])
    ready = next(group for group in review["groups"] if group["kind"] == "assembly")
    post_parse = ready["evidence"]["post_parse"]
    assert post_parse["agreement"] == "needs_review"
    assert post_parse["checks"][0]["column_values_not_in_primary"] == 1
    assert post_parse["review_reasons"] and all(reason.startswith("post_parse_") for reason in post_parse["review_reasons"])
    # A person may still approve; the system may not.
    assert review["approval_ready"]
    with pytest.raises(PackageResolutionError, match="SYSTEM_REVIEW_REQUIRES_EXCEPTION"):
        service.accept_system(plan_id=plan["plan_id"], review_manifest_sha256=review["manifest_sha256"],
                              assembly_identities=[{"name": "Board", "revision": "A"}],
                              caller_application="synthetic-quickquote")


def test_plan_refuses_a_choice_whose_result_was_changed_after_planning():
    from bom_processor.bom_package_choice import _json_bytes
    from bom_processor.package_resolution import PackageResolutionError
    store_, service, plan, choice = chosen_package()
    path = f"package-choice/{choice['request_id']}/result.json"
    result = json.loads(store_.registry[path])
    result["primary_selections"] = []
    store_.registry[path] = _json_bytes(result)
    with pytest.raises(PackageResolutionError, match="PACKAGE_CHOICE_BINDING"):
        service.review(plan["plan_id"])


def test_api_returns_202_package_choice_pending_with_the_request_identity():
    from deterministic_api import ProcessingApi
    from test_deterministic_api import config, headers

    class Pending:
        def process(self, request):
            raise PackageChoicePendingError("c" * 64, "started")

    request = make_request()
    body = json.dumps({"job_id": "job-1", "customer_key": "cust", "parser_key": "Parser1", "rfq_uuid": RFQ,
                       "package_version": "1",
                       "documents": [{"document_id": d.document_id, "blob_path": d.blob_path,
                                      "size_bytes": d.size_bytes, "sha256": d.sha256} for d in request.documents],
                       "package_files": [{"document_id": f.document_id, "file_name": f.file_name,
                                          "blob_path": f.blob_path, "size_bytes": f.size_bytes, "sha256": f.sha256}
                                         for f in request.package_files]}).encode()
    status, payload = ProcessingApi(config(max_body=100_000), lambda: Pending()).handle(headers(), body)
    assert status == 202
    assert payload == {"version": "1", "status": "package_choice_pending", "job_id": "job-1",
                       "request_fingerprint": request.fingerprint(),
                       "package_choice": {"request_id": "c" * 64, "state": "started"}}


def test_survey_finds_every_package_file_by_hash():
    view = build_view(expand_package([("package.zip", PACKAGE)]))
    hashes = {item.sha256 for item in view.files.values()}
    assert {hashlib.sha256(data).hexdigest() for data in DOCS.values()} <= hashes
