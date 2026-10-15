"""agentquote-job-line.v1: one START and one END line per job naming its quote and BOM (QQ #121).

Synthetic data only. Run python -m pytest tests/test_job_identity_log.py.
"""
import json
import sys
from datetime import datetime, timezone
from types import ModuleType, SimpleNamespace

import pytest

from bom_processor import InMemoryStorage
from bom_processor import job_identity_log as job_log
from bom_processor.bom_package_choice import REQUEST_VERSION_V2, RESULT_VERSION_V2, _hash, _json_bytes

AT = datetime(2026, 10, 15, 1, 2, 3, tzinfo=timezone.utc)
RFQ = "11111111-2222-4333-8444-555555555555"
OTHER_RFQ = "99999999-2222-4333-8444-555555555555"
ASSEMBLY = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
PACKAGE_FILE = "bbbbbbbb-bbbb-4ccc-8ddd-eeeeeeeeeeee"
SHA = "1" * 64
REQUEST = "request-1"
STAGED = f"cust-1/rfq-{RFQ}/package-1/original-boms/doc-1-synthetic.xlsx"
BASE = {"schema", "event", "phase", "job", "at", "result", "reason_code", "reason", *job_log.IDENTITY_FIELDS}
LEAK = "C:\\Users\\someone\\secret-folder\\bom.xlsx https://acct.blob.example/boms/x.xlsx?sig=TOKEN"


def choice_records():
    body = {"version": REQUEST_VERSION_V2, "customer_key": "cust-1", "rfq_uuid": RFQ, "package_version": "1",
            "package_files": [{"document_id": PACKAGE_FILE, "file_name": "Synthetic BOM.xlsx",
                               "blob_path": f"evidence-inputs/{RFQ}/package-1/{PACKAGE_FILE}/{SHA}",
                               "size_bytes": 10, "sha256": SHA}],
            "documents": [{"document_id": "doc-1", "blob_path": STAGED, "size_bytes": 10, "sha256": SHA}],
            "requested_assemblies": [{"assembly_id": ASSEMBLY, "part_number": "SYN-100", "revision": "A"}]}
    request = {**body, "request_id": _hash(body)}
    result = {"version": RESULT_VERSION_V2, "request_id": request["request_id"],
              "documents": [{"document_id": "doc-1", "sha256": SHA, "decision": "process", "reason": "only_bom",
                             "requested_assembly_ids": [ASSEMBLY]}],
              "requested_assemblies": body["requested_assemblies"]}
    return request, result


def candidate_request(choice_request=None, result=None, **changes):
    value = {"version": "candidate-request.v2", "request_id": REQUEST, "parser_key": "parser-1",
             "customer_key": "cust-1", "profile_hash": "2" * 64, "status": "requested",
             "source_evidence": [{"document_id": "doc-1", "blob_path": STAGED, "size_bytes": 10, "sha256": SHA}]}
    if choice_request is not None:
        value["package_choice"] = {"request_id": choice_request["request_id"], "result_sha256": _hash(result)}
    return {**value, **changes}


def stored(with_choice=True):
    storage = InMemoryStorage()
    choice, result = choice_records()
    if with_choice:
        base = f"package-choice/{choice['request_id']}"
        storage.upload_registry_immutable(f"{base}/request.json", _json_bytes(choice), "application/json")
        storage.upload_registry_immutable(f"{base}/result.json", _json_bytes(result), "application/json")
        storage.upload_registry_immutable(f"{base}/completed.json", _json_bytes(
            {"request_id": choice["request_id"], "result_sha256": _hash(result)}), "application/json")
    request = candidate_request(choice if with_choice else None, result)
    storage.upload_registry_immutable(f"candidates/requests/{REQUEST}.json", json.dumps(request).encode(),
                                      "application/json")
    return storage, choice


def recorder():
    lines = []
    return lines, (lambda text: lines.append(json.loads(text)))


# Line shape.

@pytest.mark.parametrize("job", sorted(job_log.JOBS))
def test_start_and_end_lines_have_every_field_with_nulls_when_unknown(job):
    start = job_log.job_line("start", job, None, at=AT)
    end = job_log.job_line("end", job, None, at=AT, result="failed", reason_code="SCHEMA_INVALID")
    expected = BASE | ({"worker_job_id"} if job == "parser_worker" else set())
    assert set(start) == set(end) == expected
    assert start["schema"] == "agentquote-job-line.v1" and start["event"] == "agentquote_job"
    assert start["at"] == "2026-10-15T01:02:03Z" and (start["phase"], end["phase"]) == ("start", "end")
    assert all(start[field] is None for field in job_log.IDENTITY_FIELDS)
    assert (start["result"], start["reason_code"], start["reason"]) == (None, None, None)
    assert end["result"] == "failed" and end["reason_code"] == "SCHEMA_INVALID"
    assert end["reason"] == "The parser output does not have the required columns."
    rendered = job_log.render(start)
    assert "\n" not in rendered and json.loads(rendered) == start


def test_invalid_phase_job_or_result_is_refused():
    for args, kwargs in ((("middle", "author"), {}), (("start", "builder"), {}),
                         (("start", "author"), {"result": "succeeded"}), (("end", "author"), {}),
                         (("end", "author"), {"result": "done"})):
        with pytest.raises(ValueError):
            job_log.job_line(*args, None, at=AT, **kwargs)


def test_reason_words_for_known_unknown_and_absent_codes():
    assert job_log.reason_text("CHOICE_WORKER_FAILED") == "The package choice job stopped on an error."
    assert job_log.reason_text("candidate_authoring_failed") == "Writing a parser for this BOM failed."
    assert job_log.reason_text("NOT_A_KNOWN_CODE") == job_log.UNKNOWN_REASON
    assert job_log.reason_text(None) is None
    assert job_log.job_line("end", "choice", None, at=AT, result="failed")["reason"] == job_log.UNKNOWN_REASON
    assert job_log.job_line("end", "choice", None, at=AT, result="succeeded")["reason"] is None
    skipped = job_log.job_line("end", "choice", None, at=AT, result="skipped", reason_code=job_log.NOT_RUN)
    assert skipped["reason_code"] == "JOB_NOT_RUN" and skipped["reason"].startswith("This run did nothing")


def test_no_path_url_or_exception_message_reaches_a_line():
    line = job_log.job_line("end", "parser_worker", {
        "request_id": "C:\\path\\x", "source_file": "https://acct.blob.example/boms/x.xlsx?sig=TOKEN",
        "assembly": "line\none", "package_plan_id": "../up"}, at=AT, result="failed", reason_code=LEAK,
        worker_job_id="https://acct.example/job")
    text = job_log.render(line)
    for fragment in ("secret", "sig=", "TOKEN", "https", "C:\\", "Users", "../"):
        assert fragment not in text
    assert line["reason_code"] is None and line["reason"] == job_log.UNKNOWN_REASON
    assert job_log.file_name("cust/rfq-x/package-1/original-boms/doc-1-a.xlsx") == "doc-1-a.xlsx"
    assert job_log.file_name("folder\\nested\\b.csv") == "b.csv"


def test_job_log_prints_each_line_once_and_start_before_end():
    lines, write = recorder()
    log = job_log.JobLog("choice", now=lambda: AT, write=write)
    log.end("failed", "INVALID_ENVIRONMENT")
    log.end("succeeded")
    log.start()
    assert [line["phase"] for line in lines] == ["start", "end"]
    assert lines[1]["reason_code"] == "INVALID_ENVIRONMENT"


def test_identity_or_writer_errors_never_escape():
    def broken_identity():
        raise RuntimeError(LEAK)
    lines, write = recorder()
    log = job_log.JobLog("author", now=lambda: AT, write=write)
    log.note(request_id=REQUEST)
    log.start(broken_identity)
    log.end("succeeded", candidate_id="candidate-1")
    assert lines[0]["request_id"] == REQUEST and lines[0]["candidate_id"] is None
    assert lines[1]["candidate_id"] == "candidate-1"

    def failing_write(_):
        raise OSError("closed")
    quiet = job_log.JobLog("author", write=failing_write)
    quiet.start()
    quiet.end("failed", "candidate_job_failed")


# Identity from stored records.

def test_candidate_identity_from_stored_request_choice_and_result():
    storage, choice = stored()
    context = job_log.load_candidate_context(storage, REQUEST)
    assert context["request"]["request_id"] == REQUEST and context["choice_request"] == choice
    assert context["choice_result"]["documents"][0]["requested_assembly_ids"] == [ASSEMBLY]
    assert context["membership"] is None
    identity = job_log.candidate_identity(REQUEST, context["request"], candidate_id="candidate-1",
                                          choice_request=context["choice_request"],
                                          choice_result=context["choice_result"], membership=context["membership"])
    assert identity == {"request_id": REQUEST, "rfq_uuid": RFQ, "package_version": "1", "package_plan_id": None,
                        "assembly_id": ASSEMBLY, "assembly": "SYN-100", "source_file": "Synthetic BOM.xlsx",
                        "candidate_id": "candidate-1", "attempt": None, "max_attempts": None}


def test_candidate_identity_without_choice_uses_staged_name_and_namespace():
    storage, _ = stored(with_choice=False)
    context = job_log.load_candidate_context(storage, REQUEST)
    identity = job_log.candidate_identity(REQUEST, context["request"])
    assert (identity["rfq_uuid"], identity["package_version"]) == (RFQ, "1")
    assert identity["source_file"] == "doc-1-synthetic.xlsx"
    assert identity["assembly_id"] is None and identity["assembly"] is None


def test_plan_comes_from_budget_membership_and_disagreement_is_null():
    choice, result = choice_records()
    request = candidate_request(choice, result)
    member = {"plan_id": "3" * 64}
    agree = job_log.candidate_identity(REQUEST, request, choice_request=choice, choice_result=result,
                                       membership=(member, {"identity": {"rfq_uuid": RFQ, "package_version": "1"}}))
    assert agree["package_plan_id"] == "3" * 64 and agree["rfq_uuid"] == RFQ
    differ = job_log.candidate_identity(REQUEST, request, choice_request=choice, choice_result=result,
                                        membership=(member, {"identity": {"rfq_uuid": OTHER_RFQ,
                                                                         "package_version": "1"}}))
    assert differ["rfq_uuid"] is None and differ["package_version"] == "1"


def test_several_sources_with_different_files_or_assemblies_stay_null():
    choice, result = choice_records()
    second = {"document_id": "doc-2", "blob_path": STAGED.replace("doc-1", "doc-2"), "size_bytes": 5, "sha256": "4" * 64}
    request = candidate_request(choice, result)
    request["source_evidence"].append(second)
    identity = job_log.candidate_identity(REQUEST, request, choice_request=choice, choice_result=result)
    assert identity["source_file"] is None and identity["assembly_id"] is None and identity["rfq_uuid"] == RFQ
    one = job_log.candidate_identity(REQUEST, request, choice_request=choice, choice_result=result, sha256="4" * 64)
    assert one["source_file"] == "doc-2-synthetic.xlsx" and one["assembly_id"] is None


def test_identity_read_failure_yields_nulls():
    class Broken:
        def download_registry(self, path, limit):
            raise RuntimeError(LEAK)
    for storage in (object(), Broken()):
        context = job_log.load_candidate_context(storage, REQUEST)
        assert context == {"request": None, "choice_request": None, "choice_result": None, "membership": None}
        assert job_log.load_choice_identity(storage, "5" * 64) == job_log.make_identity(request_id="5" * 64)
        assert job_log.load_evidence_identity(storage, "5" * 64) == job_log.make_identity(request_id="5" * 64)
        assert job_log.load_budget_claim(storage, REQUEST, "6" * 64, "1", "7" * 64) is None


def test_author_attempts_and_limits():
    from bom_processor.author_budget import LIMIT
    from bom_processor.candidate_automation import CandidateAutomationService
    assert job_log.author_job(("author", 2)) == ("author", 2, CandidateAutomationService.MAX_ATTEMPTS)
    # The refinement limit is configurable in the API process, so the job does not know it.
    assert job_log.author_job(("author-refinement", 1)) == ("author_refinement", 1, None)
    assert job_log.author_job(None, {"phase": "author-refinement", "slot": 4}) == ("author_refinement", 4, LIMIT)
    assert job_log.author_job(None, refinement=True) == ("author_refinement", None, None)
    assert job_log.author_job(None) == ("author", None, None)


def test_choice_and_evidence_identity():
    choice, _ = choice_records()
    assert job_log.choice_identity(choice["request_id"], choice) == job_log.make_identity(
        request_id=choice["request_id"], rfq_uuid=RFQ, package_version="1")
    request = {"version": "package-evidence-request.v4", "retry": {"retryOfRequestId": "8" * 64, "attempt": 3},
               "context": {"qqUuid": RFQ, "packageVersion": 2, "bomPlanId": "9" * 64,
                           "assemblies": [{"assemblyUuid": ASSEMBLY, "partNumber": "SYN-200", "revision": None}]}}
    identity = job_log.evidence_identity("e" * 64, request)
    assert identity == {"request_id": "e" * 64, "rfq_uuid": RFQ, "package_version": "2", "package_plan_id": "9" * 64,
                        "assembly_id": ASSEMBLY, "assembly": "SYN-200", "source_file": None, "candidate_id": None,
                        "attempt": 3, "max_attempts": 5}
    request["context"]["assemblies"].append({"assemblyUuid": PACKAGE_FILE, "partNumber": "SYN-300", "revision": None})
    assert job_log.evidence_identity("e" * 64, request)["assembly"] is None


def test_state_outcomes():
    assert job_log.state_outcome({"status": "completed"}, "X") == ("succeeded", None)
    assert job_log.state_outcome({"status": "failed"}, "PACKAGE_NO_BOM") == ("failed", "PACKAGE_NO_BOM")
    assert job_log.state_outcome({"status": "running"}, "X") == ("skipped", "JOB_NOT_RUN")


# Entry points.

def install_azure(monkeypatch, storage):
    azure = ModuleType("azure")
    identity = ModuleType("azure.identity")
    identity.ManagedIdentityCredential = lambda **_: object()
    azure.identity = identity
    monkeypatch.setitem(sys.modules, "azure", azure)
    monkeypatch.setitem(sys.modules, "azure.identity", identity)
    monkeypatch.setattr("bom_processor.core.AzureBlobStorage.from_account_url", lambda *args, **kwargs: storage)


def author_service(outcome):
    class Result:
        status = "generated_unvalidated"
        request_id = REQUEST
        candidate_id = "candidate-1"
        manifest_path = f"candidates/{REQUEST}/candidate-1/candidate-manifest.json"

    class Service:
        def __init__(self, *args, **kwargs):
            pass

        def author(self):
            if outcome is not None:
                raise outcome
            return Result()
    return Service


def run_author(monkeypatch, capsys, storage, outcome=None):
    import candidate_job
    monkeypatch.setenv("REQUEST_ID", REQUEST)
    monkeypatch.setenv("AUTHOR_ATTEMPT", "1")
    monkeypatch.setenv("AUTHOR_ATTEMPT_KIND", "author")
    monkeypatch.setenv("BOM_STORAGE_ACCOUNT_URL", "https://example.blob.core.usgovcloudapi.net")
    install_azure(monkeypatch, storage)
    monkeypatch.setattr("bom_processor.candidate_authoring.CandidateAuthoringService", author_service(outcome))
    monkeypatch.setattr("bom_processor.foundry_author.create_foundry_author", lambda: object())
    code = candidate_job.main()
    out = capsys.readouterr().out
    return code, out, [json.loads(line) for line in out.strip().splitlines()]


def test_author_job_logs_identity_then_keeps_its_final_line(monkeypatch, capsys):
    storage, _ = stored()
    code, _, lines = run_author(monkeypatch, capsys, storage)
    start, end, final = lines
    assert code == 0 and final == {"status": "generated_unvalidated", "request_id": REQUEST,
                                   "candidate_id": "candidate-1",
                                   "manifest_path": f"candidates/{REQUEST}/candidate-1/candidate-manifest.json"}
    assert (start["job"], start["phase"], start["attempt"], start["max_attempts"]) == ("author", "start", 1, 3)
    assert (start["rfq_uuid"], start["assembly"], start["source_file"]) == (RFQ, "SYN-100", "Synthetic BOM.xlsx")
    assert start["candidate_id"] is None and end["candidate_id"] == "candidate-1"
    assert (end["result"], end["reason_code"], end["reason"]) == ("succeeded", None, None)


def test_author_failure_uses_the_most_specific_code_and_leaks_no_message(monkeypatch, capsys):
    from bom_processor.candidate_authoring import AuthoringError
    storage, _ = stored()
    code, out, lines = run_author(monkeypatch, capsys, storage, AuthoringError("CANDIDATE_CODE_POLICY", "banned_import"))
    assert code == 1 and lines[1]["reason_code"] == "CANDIDATE_CODE_POLICY"
    assert lines[1]["reason"] == "The written parser broke the code safety rules."
    code, out, lines = run_author(monkeypatch, capsys, storage, RuntimeError(LEAK))
    assert code == 1 and lines[-1] == {"status": "error", "request_id": REQUEST, "error": "candidate_job_failed"}
    assert lines[1]["reason_code"] == "candidate_job_failed"
    for fragment in ("secret", "sig=", "TOKEN", "Users"):
        assert fragment not in out


def test_identity_read_failure_does_not_change_the_author_outcome(monkeypatch, capsys):
    class Broken:
        def download_registry(self, path, limit):
            raise RuntimeError(LEAK)

        def upload_registry_immutable(self, path, data, content_type):
            pass
    code, out, lines = run_author(monkeypatch, capsys, Broken())
    assert code == 0 and lines[-1]["candidate_id"] == "candidate-1" and len(lines) == 3
    assert lines[0]["request_id"] == REQUEST and lines[0]["rfq_uuid"] is None and lines[0]["source_file"] is None
    assert "secret" not in out


def test_invalid_environment_still_logs_start_and_end(monkeypatch, capsys):
    import choice_job
    monkeypatch.setenv("REQUEST_ID", REQUEST)
    monkeypatch.setenv("REQUEST_EXTRA", "x")
    assert choice_job.main() == 2
    start, end, final = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
    assert final == {"status": "error", "code": "INVALID_ENVIRONMENT"}
    assert (start["job"], end["result"], end["reason_code"]) == ("choice", "failed", "INVALID_ENVIRONMENT")
    assert start["request_id"] is None


def test_choice_job_lines(monkeypatch, capsys):
    import choice_job
    storage, choice = stored()
    monkeypatch.setenv("REQUEST_ID", choice["request_id"])
    monkeypatch.setitem(sys.modules, "deterministic_api", SimpleNamespace(_storage_factory=lambda: storage))
    monkeypatch.setattr("bom_processor.bom_package_choice.create_choice_caller", lambda: None)
    assert choice_job.main() == 0
    start, end, final = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
    assert final == {"request_id": choice["request_id"], "status": "completed"}
    assert (start["job"], start["rfq_uuid"], start["package_version"], start["assembly"]) == ("choice", RFQ, "1", None)
    assert end["result"] == "succeeded"


def test_validator_logs_parser_worker_lines_between_its_own(monkeypatch, capsys):
    import validation_job
    item = SimpleNamespace(job_id="validate-0123", input_sha256=SHA)

    class Result:
        status = "awaiting_approval"
        request_id = REQUEST
        candidate_id = "candidate-1"
        validation_id = "validation-1"
        evidence_path = f"candidates/{REQUEST}/candidate-1/validation/validation-1/evidence.json"

    class Service:
        def __init__(self, *args, worker_log=None, **kwargs):
            self.worker_log = worker_log

        def validate(self):
            self.worker_log("start", item)
            self.worker_log("end", item, "failed", "WORKER_TIMEOUT")
            return Result()

    storage, _ = stored()
    values = {"REQUEST_ID": REQUEST, "CANDIDATE_ID": "candidate-1",
              "BOM_STORAGE_ACCOUNT_URL": "https://example.blob.core.usgovcloudapi.net",
              "BOM_JOB_SUBSCRIPTION_ID": "subscription", "BOM_JOB_RESOURCE_GROUP": "rg",
              "BOM_JOB_NAME": "worker", "BOM_WORKER_IMAGE": "registry/worker:version"}
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    install_azure(monkeypatch, storage)
    for name in ("AzureSasFactory", "AzureJobStarter", "JobOrchestrator"):
        monkeypatch.setattr(f"bom_processor.job_orchestrator.{name}", lambda *args, **kwargs: object())
    monkeypatch.setattr("bom_processor.candidate_validation.CandidateValidationService", Service)
    assert validation_job.main() == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
    assert [(line.get("job"), line.get("phase")) for line in lines[:-1]] == [
        ("validator", "start"), ("parser_worker", "start"), ("parser_worker", "end"), ("validator", "end")]
    worker_start, worker_end = lines[1], lines[2]
    assert worker_start["worker_job_id"] == worker_end["worker_job_id"] == "validate-0123"
    assert (worker_start["source_file"], worker_start["assembly_id"], worker_start["candidate_id"]) == (
        "Synthetic BOM.xlsx", ASSEMBLY, "candidate-1")
    assert (worker_end["result"], worker_end["reason_code"]) == ("failed", "WORKER_TIMEOUT")
    assert "worker_job_id" not in lines[0] and lines[3]["result"] == "succeeded"
    assert lines[-1]["status"] == "awaiting_approval"


def test_validation_service_reports_each_worker_job_without_leaking_errors():
    from bom_processor.candidate_validation import CandidateValidationError
    from test_candidate_validation import Orchestrator, setup_validation

    service, storage, orchestrator, request, candidate = setup_validation()
    lines, write = recorder()
    context = job_log.load_candidate_context(storage, request.request_id)
    service.worker_log = job_log.parser_worker_log(context, request.request_id, candidate.candidate_id,
                                                   now=lambda: AT, write=write)
    assert service.validate().status == "awaiting_approval"
    start, end = lines
    assert (start["job"], start["phase"], end["phase"], end["result"]) == ("parser_worker", "start", "end", "succeeded")
    assert start["worker_job_id"] == orchestrator.calls[0].job_id and start["source_file"] == "doc.csv"
    assert start["request_id"] == request.request_id and start["candidate_id"] == candidate.candidate_id

    class Failing(Orchestrator):
        def start(self, item):
            raise RuntimeError(LEAK)

    service, storage, _, request, candidate = setup_validation()
    service.orchestrator = Failing(storage, b"")
    lines, write = recorder()
    service.worker_log = job_log.parser_worker_log({}, request.request_id, candidate.candidate_id, write=write)
    with pytest.raises(CandidateValidationError, match="WORKER_FAILED"):
        service.validate()
    assert [line["phase"] for line in lines] == ["start", "end"]
    assert (lines[1]["result"], lines[1]["reason_code"]) == ("failed", "WORKER_FAILED")
    assert "TOKEN" not in json.dumps(lines) and "secret" not in json.dumps(lines)


def test_a_broken_worker_hook_never_changes_validation():
    from test_candidate_validation import setup_validation
    service, *_ = setup_validation()

    def hook(*args):
        raise RuntimeError("log sink down")
    service.worker_log = hook
    assert service.validate().status == "awaiting_approval"
