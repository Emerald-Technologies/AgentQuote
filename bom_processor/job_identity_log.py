"""One START and one END log line per job, naming the quote and BOM it worked on (QQ #121).

Contract: agentquote-job-line.v1 (docs/JOB_IDENTITY_LOG_LINES.md). QuickQuote's `qq status --job-log`
parses these lines and joins them per quote and per BOM, so field names never change.

Inputs: the stored request a job already reads (candidate, package-choice or evidence request), the
author attempt environment and the automation's fixed attempt limits. Output: single-line JSON on
stdout. Identity holds only ids, the assembly name and the source file NAME that QuickQuote already
shows; never paths, URLs, cell values, exception messages, hashes, secrets or environment values.
Unknown values are null, never guessed. Logging never changes a job's outcome: the loaders and the
emitter swallow every error and fall back to nulls.
Run python -m pytest tests/test_job_identity_log.py.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any, Callable

SCHEMA = "agentquote-job-line.v1"
EVENT = "agentquote_job"
JOBS = frozenset({"author", "author_refinement", "validator", "parser_worker", "choice", "evidence"})
RESULTS = frozenset({"succeeded", "failed", "skipped"})
IDENTITY_FIELDS = ("request_id", "rfq_uuid", "package_version", "package_plan_id", "assembly_id", "assembly",
                   "source_file", "candidate_id", "attempt", "max_attempts")
UNKNOWN_REASON = "The job stopped with an unrecognised error."
# A run that found the request already finished or owned by another run (choice and evidence jobs).
NOT_RUN = "JOB_NOT_RUN"

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_CODE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,79}")
_SOURCE_NAMESPACE = re.compile(r"[^/]+/rfq-([0-9a-fA-F-]{36})/package-([A-Za-z0-9][A-Za-z0-9._-]{0,127})/original-boms/[^/]+")

# Plain words per stable code these jobs already emit. Unknown codes read UNKNOWN_REASON.
REASONS = {
    # Job environment and outcomes.
    "invalid_request_environment": "The job was started with missing or unexpected request settings.",
    "invalid_association_policy": "The job was started with an invalid source-association setting.",
    "invalid_author_attempt_environment": "The job was started with an invalid author attempt number.",
    "INVALID_ENVIRONMENT": "The job was started with missing or unexpected request settings.",
    "candidate_authoring_failed": "Writing a parser for this BOM failed.",
    "candidate_job_failed": "The author job stopped on an internal error.",
    "author_attempts_exhausted": "Every allowed author attempt was used without producing a parser.",
    "generated_unvalidated": "The parser ran but its output did not pass validation.",
    NOT_RUN: "This run did nothing because the request was already finished or another run owns it.",
    # Author (candidate_authoring.AuthoringError codes).
    "REQUEST_INVALID": "The stored request is not in the expected form.",
    "REQUEST_UNAVAILABLE": "The stored request could not be read.",
    "INVALID_REQUEST_ID": "The request id is not valid.",
    "SOURCE_INTEGRITY": "The BOM file no longer matches the recorded copy.",
    "SOURCE_UNAVAILABLE": "The BOM file could not be read.",
    "SOURCE_PACKAGE_LIMIT": "The BOM files are larger than the author job accepts.",
    "SOURCE_NOT_BOM": "The file does not look like a BOM.",
    "MODEL_FAILED": "The model call for writing the parser failed.",
    "CANDIDATE_CODE_INVALID": "The written parser is not valid code.",
    "CANDIDATE_CODE_POLICY": "The written parser broke the code safety rules.",
    "CANDIDATE_CONFLICT": "A different parser was already recorded for this request.",
    "AUTHORING_BUNDLE_INVALID": "The written parser bundle is not in the expected form.",
    "PACKAGE_CHOICE_BINDING": "The package choice this request depends on no longer matches.",
    "REFINEMENT_INTEGRITY": "The earlier parser or validation to improve on does not match its record.",
    "REFINEMENT_UNAVAILABLE": "The earlier parser or validation to improve on could not be read.",
    "INVALID_REFINEMENT": "The parser improvement request is not valid.",
    "DOCUMENT_ROLE_REVIEW_REQUIRED": "A person must confirm which files are BOMs before a parser is written.",
    "DOCUMENT_ROLE_COVERAGE_INVALID": "The file role evidence does not cover every file.",
    "DOCUMENT_ROLE_ARTIFACT_LIMIT": "The file role evidence is larger than allowed.",
    "ASSOCIATION_COVERAGE_INVALID": "The assembly association evidence does not cover every file.",
    "ASSOCIATION_ARTIFACT_LIMIT": "The assembly association evidence is larger than allowed.",
    "ASSEMBLY_ASSOCIATION_REVIEW_REQUIRED": "A person must confirm which assembly this BOM belongs to.",
    # Validator and parser worker (candidate_validation.CandidateValidationError codes).
    "INVALID_CANDIDATE_ID": "The parser candidate id is not valid.",
    "CANDIDATE_UNAVAILABLE": "The parser candidate could not be read.",
    "CANDIDATE_INTEGRITY": "The parser candidate does not match its record.",
    "SCHEMA_INVALID": "The parser output does not have the required columns.",
    "SEMANTIC_INVALID": "The parser output does not account for the BOM lines.",
    "VALIDATION_CONFLICT": "A different validation result was already recorded.",
    "VALIDATION_EVIDENCE_INVALID": "The recorded validation result is not in the expected form.",
    "VALIDATION_HISTORY_INVALID": "The validation history for this parser is not in the expected form.",
    "VALIDATION_POLICY_REAUTHOR_REQUIRED": "The validation rules changed; a new parser must be written.",
    "ASSEMBLY_ASSOCIATION_INVALID": "The assembly association could not be checked.",
    "ASSEMBLY_ASSOCIATION_COVERAGE_INVALID": "The assembly association does not cover every file.",
    "ASSOCIATION_ARTIFACT_INVALID": "The assembly association record is not in the expected form.",
    "ASSOCIATION_ARTIFACT_INTEGRITY": "The assembly association record does not match its hash.",
    "ASSOCIATION_POLICY_MISMATCH": "The assembly association does not match the validation settings.",
    "WORKER_FAILED": "The parser worker job could not be started or stopped unexpectedly.",
    "WORKER_EXECUTION_FAILED": "The parser worker job failed before the parser finished.",
    "WORKER_TIMEOUT": "The parser worker job did not finish in time.",
    "WORKER_PARSER_FAILED": "The parser stopped with an error while reading the BOM.",
    "WORKER_RESULT_INVALID": "The parser worker result is not in the expected form.",
    "WORKER_EVIDENCE_INVALID": "The parser worker did not confirm the exact BOM and parser it ran.",
    "WORKER_SOURCE_INTEGRITY": "The parser worker read a BOM that does not match the recorded copy.",
    "WORKER_OUTPUT_MISSING": "The parser worker wrote no output.",
    "WORKER_OUTPUT_INVALID": "The parser worker output is not in the expected form.",
    "WORKER_OUTPUT_INTEGRITY": "The parser worker output does not match its recorded hash.",
    "WORKER_ROW_MAP_MISSING": "The parser did not write its row map.",
    "WORKER_ROW_MAP_INVALID": "The parser row map is not in the expected form.",
    "WORKER_ROW_MAP_INTEGRITY": "The parser row map does not match its recorded hash.",
    "WORKER_INTERPRETATION_INVALID": "The parser interpretation is not in the expected form.",
    "WORKER_INTERPRETATION_INTEGRITY": "The parser interpretation does not match its recorded hash.",
    "WORKER_INTERPRETATION_CLAIM_MISMATCH": "The parser interpretation does not match the BOM.",
    "WORKER_INTERPRETATION_REVIEW_REQUIRED": "A person must review the parser interpretation.",
    "WORKER_INTERPRETATION_VERIFICATION_FAILED": "The parser interpretation could not be verified.",
    # Package choice.
    "CHOICE_WORKER_FAILED": "The package choice job stopped on an error.",
    "PACKAGE_CHOICE_FAILED": "The package choice stopped on an error.",
    "PACKAGE_CHOICE_NOT_FOUND": "The package choice request was not found.",
    "PACKAGE_CHOICE_ID_INVALID": "The package choice request id is not valid.",
    "PACKAGE_CHOICE_ARTIFACT_CORRUPT": "A stored package choice record is not in the expected form.",
    "PACKAGE_CHOICE_RESULT_CORRUPT": "The stored package choice result does not match its record.",
    "PACKAGE_FILE_UNAVAILABLE": "A package file could not be read.",
    "PACKAGE_FILE_IDENTITY_MISMATCH": "A package file no longer matches the recorded copy.",
    "PACKAGE_SURVEY_INCOMPLETE": "The package survey could not classify every spreadsheet; a person must review it.",
    "PACKAGE_NO_BOM": "No BOM was found in the package.",
    "PACKAGE_NO_REQUESTED_BOM": "No BOM in the package matches a requested assembly.",
    # Package evidence.
    "EVIDENCE_WORKER_FAILED": "The package evidence job stopped on an error.",
    "EVIDENCE_JOB_FAILED": "The package evidence run failed and was recorded as failed.",
    "MODEL_DISABLED": "The package evidence model is switched off for this job.",
    "POLICY_BINDING": "The evidence request was made under different model settings.",
    "MODEL_BINDING": "The configured model is not the one the evidence request names.",
    "MODEL_POLICY_BINDING": "The model settings do not match the evidence request.",
    "REQUEST_NOT_FOUND": "The evidence request was not found.",
    "REQUEST_CORRUPT": "The stored evidence request is not in the expected form.",
    "SOURCE_IDENTITY_MISMATCH": "A package file no longer matches the recorded copy.",
    "MODEL_PROPOSAL_FAILED": "The model call for the package evidence failed.",
    "UNSTRUCTURED_EVIDENCE_ERROR": "The package evidence job stopped on an error without a stable code.",
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _id(value: Any) -> str | None:
    return value if isinstance(value, str) and _ID.fullmatch(value) else None


def _uuid(value: Any) -> str | None:
    return value.lower() if isinstance(value, str) and _UUID.fullmatch(value) else None


def _text(value: Any, limit: int) -> str | None:
    if not isinstance(value, str) or not 0 < len(value) <= limit or any(ord(c) < 32 or ord(c) == 127 for c in value):
        return None
    return value


def file_name(value: Any) -> str | None:
    """The last segment of a name or blob path; None for anything that could carry a URL or query."""
    if not isinstance(value, str):
        return None
    name = value.replace("\\", "/").rsplit("/", 1)[-1]
    if any(mark in name for mark in ("?", "#", ":", "%")):
        return None
    return _text(name, 255) if name not in (".", "..") else None


def _count(value: Any) -> int | None:
    return value if type(value) is int and 1 <= value <= 1000 else None


def make_identity(*, request_id: Any = None, rfq_uuid: Any = None, package_version: Any = None,
                  package_plan_id: Any = None, assembly_id: Any = None, assembly: Any = None,
                  source_file: Any = None, candidate_id: Any = None, attempt: Any = None,
                  max_attempts: Any = None) -> dict[str, Any]:
    """Every identity field, each checked for shape; anything unexpected becomes null."""
    version = str(package_version) if type(package_version) is int else package_version
    return {"request_id": _id(request_id), "rfq_uuid": _uuid(rfq_uuid), "package_version": _id(version),
            "package_plan_id": _id(package_plan_id), "assembly_id": _uuid(assembly_id),
            "assembly": _text(assembly, 100), "source_file": file_name(source_file),
            "candidate_id": _id(candidate_id), "attempt": _count(attempt), "max_attempts": _count(max_attempts)}


def reason_text(reason_code: str | None) -> str | None:
    return None if reason_code is None else REASONS.get(reason_code, UNKNOWN_REASON)


def job_line(phase: str, job: str, identity: dict[str, Any] | None, *, at: datetime,
             result: str | None = None, reason_code: Any = None, worker_job_id: Any = None) -> dict[str, Any]:
    """One contract line. START carries no result; worker_job_id appears on parser_worker lines only."""
    if phase not in ("start", "end") or job not in JOBS:
        raise ValueError("invalid job line")
    if (phase == "start") != (result is None) or (result is not None and result not in RESULTS):
        raise ValueError("invalid job result")
    code = reason_code if phase == "end" and isinstance(reason_code, str) and _CODE.fullmatch(reason_code) else None
    reason = reason_text(code)
    if reason is None and result == "failed":
        reason = UNKNOWN_REASON
    stamp = at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = {"schema": SCHEMA, "event": EVENT, "phase": phase, "job": job, "at": stamp,
            **make_identity(**(identity or {})), "result": result, "reason_code": code, "reason": reason}
    if job == "parser_worker":
        line["worker_job_id"] = _id(worker_job_id)
    return line


def render(line: dict[str, Any]) -> str:
    return json.dumps(line, sort_keys=True)


class JobLog:
    """Prints START once and END once. START waits for the identity; END prints START first if needed.

    The END line is printed immediately BEFORE the job's existing final result line, so that line
    stays last. No error from identity reading or printing ever reaches the job.
    """

    def __init__(self, job: str, *, now: Callable[[], datetime] = _utc_now,
                 write: Callable[[str], None] | None = None, worker_job_id: str | None = None):
        self.job, self.now, self.write = job, now, write or print
        self.worker_job_id = worker_job_id
        self.identity = make_identity()
        self.started = self.ended = False

    def start(self, identify: Callable[[], tuple[str, dict[str, Any]]] | None = None) -> None:
        if self.started:
            return
        self.started = True
        if identify is not None:
            try:
                job, identity = identify()
                if job in JOBS:
                    self.job = job
                self.identity = make_identity(**identity)
            except Exception:
                pass
        self._emit("start", None, None)

    def note(self, **fields: Any) -> None:
        """Record identity fields known before or during the work (e.g. the new candidate_id)."""
        try:
            self.identity = make_identity(**{**self.identity, **fields})
        except Exception:
            pass

    def end(self, result: str, reason_code: Any = None, **learned: Any) -> None:
        if self.ended:
            return
        if learned:
            self.note(**learned)
        self.start()
        self.ended = True
        self._emit("end", result, reason_code)

    def _emit(self, phase: str, result: str | None, reason_code: Any) -> None:
        try:
            self.write(render(job_line(phase, self.job, self.identity, at=self.now(), result=result,
                                       reason_code=reason_code, worker_job_id=self.worker_job_id)))
        except Exception:
            pass


# Identity builders: pure functions over records the job already loaded.

def _one(values: list[Any], *, every: bool = False) -> Any:
    """The single known value the sources agree on, else None; every=True also needs each source known."""
    distinct = {value for value in values if value is not None}
    if len(distinct) != 1 or (every and None in values):
        return None
    return next(iter(distinct))


def candidate_identity(request_id: str, request: dict[str, Any] | None = None, *, candidate_id: str | None = None,
                       choice_request: dict[str, Any] | None = None, choice_result: dict[str, Any] | None = None,
                       membership: tuple[dict[str, Any], dict[str, Any]] | None = None, sha256: str | None = None,
                       attempt: int | None = None, max_attempts: int | None = None) -> dict[str, Any]:
    """Identity of an author, validator or parser-worker job from its stored candidate request.

    rfq_uuid/package_version: the package-choice request, the author-budget plan identity and the
    sources' QuickQuote namespace must agree. package_plan_id: the author-budget membership only.
    assembly_id/assembly: every source document confirmed for the same one requested assembly in the
    bound package-choice result. source_file: the package file name (else the staged file name) when all
    sources share one. sha256 narrows the sources to one fixture (one parser-worker job).
    """
    sources = [item for item in (request or {}).get("source_evidence") or [] if isinstance(item, dict)]
    if sha256 is not None:
        sources = [item for item in sources if str(item.get("sha256", "")).lower() == sha256.lower()]
    quotes, versions = [], []
    if choice_request is not None:
        quotes.append(_uuid(choice_request.get("rfq_uuid")))
        versions.append(_id(choice_request.get("package_version")))
    if membership is not None:
        plan_identity = membership[1].get("identity") or {}
        quotes.append(_uuid(plan_identity.get("rfq_uuid")))
        versions.append(_id(plan_identity.get("package_version")))
    for item in sources:
        match = _SOURCE_NAMESPACE.fullmatch(str(item.get("blob_path", "")))
        quotes.append(_uuid(match.group(1)) if match else None)
        versions.append(match.group(2) if match else None)
    names = {}
    for item in (choice_request or {}).get("package_files") or []:
        names.setdefault(str(item.get("sha256", "")).lower(), set()).add(item.get("file_name"))
    files = []
    for item in sources:
        matched = names.get(str(item.get("sha256", "")).lower(), set())
        files.append(file_name(next(iter(matched)) if len(matched) == 1 else item.get("blob_path")))
    assembly_id = assembly = None
    if choice_result is not None and sources:
        documents = {item.get("document_id"): item for item in choice_result.get("documents") or []}
        links = [tuple(documents.get(item.get("document_id"), {}).get("requested_assembly_ids") or ()) for item in sources]
        if len(set(links)) == 1 and len(links[0]) == 1:
            assembly_id = links[0][0]
            requested = {item.get("assembly_id"): item for item in (choice_request or {}).get("requested_assemblies") or []}
            assembly = (requested.get(assembly_id) or {}).get("part_number")
    return make_identity(request_id=request_id, rfq_uuid=_one(quotes), package_version=_one(versions),
                         package_plan_id=None if membership is None else membership[0].get("plan_id"),
                         assembly_id=assembly_id, assembly=assembly,
                         source_file=_one(files, every=True), candidate_id=candidate_id,
                         attempt=attempt, max_attempts=max_attempts)


def author_job(attempt_context: tuple[str, int] | None, budget_claim: dict[str, Any] | None = None,
               *, refinement: bool = False) -> tuple[str, int | None, int | None]:
    """Job kind, attempt and attempt limit for an author run.

    Automation attempts: initial authoring is limited by CandidateAutomationService.MAX_ATTEMPTS (fixed);
    the refinement limit is configurable in the API process and unknown here, so it stays null.
    Budgeted authoring: the group's budget slot and its fixed limit.
    """
    if attempt_context is not None:
        kind, attempt = attempt_context
        limit = None
        if kind == "author":
            from .candidate_automation import CandidateAutomationService
            limit = CandidateAutomationService.MAX_ATTEMPTS
        return kind.replace("-", "_"), attempt, limit
    if budget_claim is not None:
        from .author_budget import LIMIT
        return budget_claim["phase"].replace("-", "_"), budget_claim["slot"], LIMIT
    return ("author_refinement" if refinement else "author"), None, None


def choice_identity(request_id: str, choice_request: dict[str, Any] | None) -> dict[str, Any]:
    """A package-choice job covers the whole package: quote and package version only."""
    value = choice_request or {}
    return make_identity(request_id=request_id, rfq_uuid=value.get("rfq_uuid"),
                         package_version=value.get("package_version"))


def evidence_identity(request_id: str, request: dict[str, Any] | None) -> dict[str, Any]:
    """Quote, package, BOM plan, the assembly when the request has exactly one, and the attempt."""
    if request is None:
        return make_identity(request_id=request_id)
    from .package_evidence_retry import MAX_ATTEMPT as RETRY_MAX, request_attempt
    from .package_evidence_successor import MAX_ATTEMPT as SUCCESSOR_MAX
    context = request.get("context") or {}
    assemblies = context.get("assemblies") or []
    single = assemblies[0] if len(assemblies) == 1 else {}
    # Only retained-provenance requests (v2 and later) can be retried or succeeded; v1 has no known limit.
    if "successor" in request:
        limit = SUCCESSOR_MAX
    elif request.get("version") != "package-evidence-request.v1":
        limit = RETRY_MAX
    else:
        limit = None
    return make_identity(request_id=request_id, rfq_uuid=context.get("qqUuid"),
                         package_version=context.get("packageVersion"), package_plan_id=context.get("bomPlanId"),
                         assembly_id=single.get("assemblyUuid"), assembly=single.get("partNumber"),
                         attempt=request_attempt(request), max_attempts=limit)


def state_outcome(state: dict[str, Any], failed_code: str | None) -> tuple[str, str | None]:
    """END result for a choice or evidence run from the state the service returned."""
    status = state.get("status")
    if status == "completed":
        return "succeeded", None
    if status == "failed":
        return "failed", failed_code
    return "skipped", NOT_RUN


# Loaders: read-only, never raise; every unreadable part is simply absent.

def load_candidate_context(storage: Any, request_id: str) -> dict[str, Any]:
    context: dict[str, Any] = {"request": None, "choice_request": None, "choice_result": None, "membership": None}
    if _id(request_id) is None:
        return context
    try:
        request = json.loads(storage.download_registry(f"candidates/requests/{request_id}.json", 1_000_000))
        if isinstance(request, dict) and request.get("request_id") == request_id:
            context["request"] = request
    except Exception:
        pass
    reference = (context["request"] or {}).get("package_choice")
    if isinstance(reference, dict):
        try:
            from .bom_package_choice import PackageChoiceService
            service = PackageChoiceService(storage)
            context["choice_request"] = service.request(str(reference.get("request_id")))
            state = service.status(str(reference.get("request_id")))
            if state.get("status") == "completed" and state.get("result_sha256") == reference.get("result_sha256"):
                context["choice_result"] = state["result"]
        except Exception:
            pass
    try:
        from .author_budget import _membership
        context["membership"] = _membership(storage, request_id)
    except Exception:
        pass
    return context


def load_budget_claim(storage: Any, request_id: str, group_id: str, slot: str,
                      reservation_hash: str) -> dict[str, Any] | None:
    """Phase and slot of this run's author-budget reservation, when the claim matches the environment."""
    try:
        from .author_budget import LIMIT, _read, _slot_path
        number = int(slot)
        claim = _read(storage, _slot_path(group_id, number, "claim"))
        if (isinstance(claim, dict) and claim.get("request_id") == request_id and claim.get("slot") == number
                and claim.get("reservation_hash") == reservation_hash and 1 <= number <= LIMIT
                and claim.get("phase") in ("author", "author-refinement")):
            return {"phase": claim["phase"], "slot": number}
    except Exception:
        pass
    return None


def load_choice_identity(storage: Any, request_id: str) -> dict[str, Any]:
    try:
        from .bom_package_choice import PackageChoiceService
        return choice_identity(request_id, PackageChoiceService(storage).request(request_id))
    except Exception:
        return make_identity(request_id=request_id)


def load_evidence_identity(storage: Any, request_id: str) -> dict[str, Any]:
    try:
        from .package_evidence_jobs import EvidenceJobService
        return evidence_identity(request_id, EvidenceJobService(storage).request(request_id))
    except Exception:
        return make_identity(request_id=request_id)


def parser_worker_log(context: dict[str, Any], request_id: str, candidate_id: str, *,
                      now: Callable[[], datetime] = _utc_now,
                      write: Callable[[str], None] | None = None) -> Callable[..., None]:
    """Validator hook: START/END lines for each parser-worker job it dispatches (worker stdout stays one line).

    context is the validator's loaded candidate context (filled when its own START line is printed).
    """
    logs: dict[str, JobLog] = {}

    def event(phase: str, item: Any, result: str | None = None, reason_code: str | None = None) -> None:
        job_id = getattr(item, "job_id", None)
        if phase == "start":
            log = logs[job_id] = JobLog("parser_worker", now=now, write=write, worker_job_id=job_id)
            log.start(lambda: ("parser_worker", candidate_identity(
                request_id, context.get("request"), candidate_id=candidate_id,
                choice_request=context.get("choice_request"), choice_result=context.get("choice_result"),
                membership=context.get("membership"), sha256=getattr(item, "input_sha256", None))))
        elif job_id in logs:
            logs.pop(job_id).end(result or "failed", reason_code)
    return event
