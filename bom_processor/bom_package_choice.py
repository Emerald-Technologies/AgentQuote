"""Package choice job (plan 8.8 step 2): survey the package, choose one primary BOM per contested
assembly, and decide which request BOM documents are processed and which are set aside.

Inputs: a ProcessingRequest that carries package_files (uploaded root files staged by QuickQuote
under evidence-inputs/). Output: immutable artifacts under package-choice/<request_id>/ in the
registry container - request.json, start/worker markers, result.json and completed.json (written
last) or failed.json. Model calls run only in the job (choice_job.py), never in the API process.

Decisions are fail-safe: a document is set aside only when the survey has no blocking finding
and either the survey calls it a non-BOM (plan 8.2-2) or the agent's primary choice for its
assembly agreed with the column-read cross-check (plan 8.2-8). Anything else is processed as
before, so a doubtful choice reaches the existing human review instead of dropping a file.
With requested assemblies (v2, round 2), BOMs are grouped by their code-confirmed link to a
requested assembly, and a BOM linked to none is set aside as not_mapped, reopenable by a
person (Nick, D2, 2026-10-10).
Run python -m pytest tests/test_bom_package_choice.py.
"""
from __future__ import annotations

import json
import re
import uuid
from typing import Any, Callable

from .core import BlobConflictError, BlobNotFoundError, ProcessingRequest

REQUEST_VERSION = "bom-package-choice-request.v1"
RESULT_VERSION = "bom-package-choice.v1"
# Round 2 (Nick 2026-10-10): requests that carry requested_assemblies link files to them; v1 stays readable.
REQUEST_VERSION_V2 = "bom-package-choice-request.v2"
RESULT_VERSION_V2 = "bom-package-choice.v2"
RESULT_VERSIONS = {REQUEST_VERSION: RESULT_VERSION, REQUEST_VERSION_V2: RESULT_VERSION_V2}
MAX_METADATA = 8 * 1024 * 1024
_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,79}")
_HASH = re.compile(r"[0-9a-f]{64}")

Caller = Callable[[str, str], Any]


class PackageChoiceError(ValueError):
    """Stable failure code without customer values."""


class PackageChoicePendingError(Exception):
    """The request carries package_files and its choice is not completed yet."""

    def __init__(self, request_id: str, state: str, code: str | None = None):
        self.request_id, self.state, self.code = request_id, state, code
        super().__init__("package choice pending")


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False).encode("utf-8")


def _hash(value: Any) -> str:
    import hashlib
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def choice_request(request: ProcessingRequest) -> dict[str, Any]:
    """Request identity: the package files, the BOM documents and any requested assemblies."""
    request.validate()
    if request.package_files is None:
        raise PackageChoiceError("PACKAGE_FILES_REQUIRED")
    linked = request.requested_assemblies is not None
    body = {"version": REQUEST_VERSION_V2 if linked else REQUEST_VERSION, "customer_key": request.customer_key,
            "rfq_uuid": request.rfq_uuid.lower(), "package_version": request.package_version,
            "package_files": [{"document_id": item.document_id, "file_name": item.file_name,
                               "blob_path": item.blob_path, "size_bytes": item.size_bytes,
                               "sha256": item.sha256} for item in request.package_files],
            "documents": [{"document_id": item.document_id, "blob_path": item.blob_path,
                           "size_bytes": item.size_bytes, "sha256": item.sha256.lower()}
                          for item in request.documents]}
    if linked:
        body["requested_assemblies"] = [{"assembly_id": item.assembly_id, "part_number": item.part_number,
                                         "revision": item.revision} for item in request.requested_assemblies]
    return {**body, "request_id": _hash(body)}


def _stable_code(exc: BaseException) -> str:
    message = str(exc)
    return message if _CODE.fullmatch(message) else "PACKAGE_CHOICE_FAILED"


def decide(request: dict[str, Any], roots: list[tuple[str, bytes]], call: Caller) -> dict[str, Any]:
    """Survey, choose and decide per request document. Pure apart from model calls."""
    from .bom_candidate_choice import ATTEMPTS, ChoiceError, run_choice, set_aside_digests
    from .bom_candidate_view import build_candidate_view
    from .package_survey import build_view, expand_package
    from .package_survey_run import survey_view

    linked = "requested_assemblies" in request
    view = build_view(expand_package(roots), request.get("requested_assemblies"))
    survey = survey_view(view, call)
    calls = survey["model_calls"]
    choices = []
    failed_files = set()
    failures = [item for item in survey["findings"] if item["raised_by"] == "code"
                and item["code"] in {"SURVEY_BATCH_FAILED", "SURVEY_CALL_LIMIT", "SURVEY_FILES_UNCLASSIFIED"}]
    # An unclassified spreadsheet makes all choice results unusable; avoid paid calls. Unclassified
    # non-spreadsheet files are a warning and do not stop the choice.
    blocked = any(item["severity"] == "blocking" for item in failures)
    for group in ([] if blocked else survey["requested_assemblies"] if linked else survey["assemblies"]):
        boms = group["files"]["bom"]
        if len(boms) < 2:
            continue
        name = group["part_number"] if linked else group["name"]
        owner = {"assembly_id": group["assembly_id"]} if linked else {}
        candidate_view = build_candidate_view(view, name, boms)
        try:
            choice, used = run_choice(candidate_view, call)
        except ChoiceError as exc:
            calls += ATTEMPTS
            failed_files.update(boms)
            failures.append({"code": "CHOICE_ASSEMBLY_FAILED", "severity": "warning", "raised_by": "code",
                             "rationale": "No primary BOM was chosen after two attempts; all BOMs of this assembly are retained for review.",
                             "file_ids": list(boms), "assembly": name})
            choices.append({"assembly": name, **owner, "view_sha256": candidate_view.sha256,
                            "choice": None, "set_aside_digests": {}, "failure_code": _stable_code(exc)})
            continue
        calls += used
        choices.append({"assembly": name, **owner, "view_sha256": candidate_view.sha256,
                        "choice": choice, "set_aside_digests": set_aside_digests(choice, candidate_view)})
    records = {record["file_id"]: record for record in survey["files"]}
    by_hash: dict[str, list[str]] = {}
    for file_id, item in view.files.items():
        by_hash.setdefault(item.sha256, []).append(file_id)
    labels: dict[str, tuple[dict[str, Any], str]] = {}
    for entry in choices:
        if entry["choice"] is None:
            continue
        for item in entry["choice"]["files"]:
            labels[item["file_id"]] = (entry, item["disposition"])
    documents, findings = [], []
    for document in request["documents"]:
        file_ids = sorted(by_hash.get(document["sha256"], []), key=lambda value: int(value[1:]))
        roles = sorted({records[file_id]["role"] for file_id in file_ids})
        # Requested assemblies this document's copies are confirmed to belong to (v2 only).
        links = sorted({records[file_id]["requested_assembly"]["assembly_id"] for file_id in file_ids
                        if (records[file_id].get("requested_assembly") or {}).get("link") == "confirmed"})
        decision, reason, disposition = "process", "only_bom", None
        if not file_ids:
            reason = "not_in_package"
            findings.append({"code": "CHOICE_DOCUMENT_NOT_IN_PACKAGE", "severity": "warning",
                             "rationale": "A BOM document of the request is not among the package files; it is processed.",
                             "document_id": document["document_id"], "raised_by": "code"})
        elif survey["has_blocking_findings"]:
            reason = "survey_blocking"
        elif failed_files.intersection(file_ids):
            reason = "choice_failed"
        elif "bom" not in roles:
            decision, reason = "excluded", roles[0] if len(roles) == 1 else "not_bom"
        elif linked and not links:
            # D2: a BOM of no requested assembly is set aside, never parsed; a person can reopen it.
            decision, reason = "excluded", "not_mapped"
        elif linked and len(links) > 1:
            # Copies confirmed for different requested assemblies: not proven, a person decides.
            reason = "links_not_consistent"
        else:
            labelled = [labels[file_id] for file_id in file_ids if file_id in labels]
            if labelled:
                agreed = all(entry["choice"]["automatic_selection_allowed"] for entry, _ in labelled)
                values = sorted({value for _, value in labelled})
                if not agreed:
                    reason = "choice_needs_review"
                elif "primary" in values:
                    # Identical bytes that are the primary anywhere stay in.
                    reason, disposition = "primary", "primary"
                elif len(labelled) < len(file_ids) or len(values) != 1:
                    # A copy outside the choice, or copies labelled differently: not proven.
                    reason = "labels_not_consistent"
                else:
                    decision, reason, disposition = "excluded", values[0], values[0]
        assemblies = sorted({records[file_id]["assembly"]["name"] for file_id in file_ids
                             if records[file_id]["assembly"] is not None})
        documents.append({"document_id": document["document_id"], "sha256": document["sha256"],
                          "decision": decision, "reason": reason, "disposition": disposition,
                          "survey_file_ids": file_ids, "roles": roles, "assemblies": assemblies,
                          **({"requested_assembly_ids": links} if linked else {})})
    processed = [item["document_id"] for item in documents if item["decision"] == "process"]
    if not processed:
        raise PackageChoiceError("PACKAGE_NO_REQUESTED_BOM" if any(item["reason"] == "not_mapped" for item in documents)
                                 else "PACKAGE_NO_BOM")
    if linked:
        failures.extend(_link_findings(documents, records))
    primaries = []
    for entry in choices:
        if entry["choice"] is None or not entry["choice"]["automatic_selection_allowed"]:
            continue
        ids = {file_id for item in entry["choice"]["files"] for file_id in [item["file_id"]]}
        members = [item for item in documents if set(item["survey_file_ids"]) & ids]
        primaries.append({"assembly": entry["assembly"],
                          "primary_document_ids": sorted(item["document_id"] for item in members
                                                         if item["decision"] == "process"),
                          "excluded_document_ids": sorted(item["document_id"] for item in members
                                                          if item["decision"] == "excluded")})
    for failure in failures:
        ids = set(failure["file_ids"])
        findings.append({key: failure[key] for key in ("code", "severity", "rationale", "raised_by")} | {
            "document_ids": sorted(item["document_id"] for item in documents if ids.intersection(item["survey_file_ids"])),
            "assemblies": sorted({records[file_id]["assembly"]["name"] for file_id in ids
                                  if records[file_id]["assembly"] is not None}),
            "file_paths": [view.files[file_id].path for file_id in sorted(ids)]})
    result = {"version": RESULT_VERSION_V2 if linked else RESULT_VERSION, "request_id": request["request_id"],
              "model_calls": calls, "survey": survey, "choices": choices, "documents": documents,
              "primary_selections": primaries, "findings": findings}
    if linked:
        result["requested_assemblies"] = request["requested_assemblies"]
    return result


def _link_findings(documents: list[dict[str, Any]], records: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Package warnings for BOMs set aside as not_mapped; each stops automatic acceptance."""
    groups: dict[str, list[str]] = {"SURVEY_BOM_NOT_MAPPED": [], "SURVEY_MAPPING_UNCONFIRMED": []}
    for document in documents:
        if document["reason"] != "not_mapped":
            continue
        files = [records[file_id] for file_id in document["survey_file_ids"] if records[file_id]["role"] == "bom"]
        unconfirmed = any((record.get("requested_assembly") or {}).get("link") == "unconfirmed" for record in files)
        groups["SURVEY_MAPPING_UNCONFIRMED" if unconfirmed else "SURVEY_BOM_NOT_MAPPED"].extend(
            record["file_id"] for record in files)
    rationale = {
        "SURVEY_BOM_NOT_MAPPED": "These BOM files belong to no requested assembly; they are set aside and a person can bring one back.",
        "SURVEY_MAPPING_UNCONFIRMED": ("These BOM files were linked to a requested assembly that no cited text confirms; "
                                       "they are set aside and a person can bring one back.")}
    findings = [{"code": code, "severity": "warning", "raised_by": "code", "rationale": rationale[code],
                 "file_ids": sorted(set(ids), key=lambda value: int(value[1:]))}
                for code, ids in groups.items() if ids]
    return findings


class PackageChoiceService:
    """Immutable request/marker/result artifacts, same pattern as EvidenceJobService.

    starter.start receives only REQUEST_ID. No retries or claim stealing; an ambiguous start or
    an interrupted worker needs explicit reconciliation, never a silent second model run.
    """

    def __init__(self, storage: Any, starter: Any = None):
        self.storage, self.starter = storage, starter

    @staticmethod
    def _path(request_id: str, artifact: str) -> str:
        if not _HASH.fullmatch(str(request_id)):
            raise PackageChoiceError("PACKAGE_CHOICE_ID_INVALID")
        return f"package-choice/{request_id}/{artifact}.json"

    def _write(self, request_id: str, artifact: str, value: dict) -> None:
        self.storage.upload_registry_immutable(self._path(request_id, artifact), _json_bytes(value),
                                               "application/json")

    def _read(self, request_id: str, artifact: str) -> dict | None:
        try:
            raw = self.storage.download_registry(self._path(request_id, artifact), MAX_METADATA)
        except BlobNotFoundError:
            return None
        try:
            value = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeError) as exc:
            raise PackageChoiceError("PACKAGE_CHOICE_ARTIFACT_CORRUPT") from exc
        if type(value) is not dict or _json_bytes(value) != raw:
            raise PackageChoiceError("PACKAGE_CHOICE_ARTIFACT_CORRUPT")
        return value

    def submit(self, request: ProcessingRequest) -> dict[str, Any]:
        record = choice_request(request)
        try:
            self._write(record["request_id"], "request", record)
        except BlobConflictError:
            if self._read(record["request_id"], "request") != record:
                raise PackageChoiceError("PACKAGE_CHOICE_CONFLICT") from None
        return record

    def request(self, request_id: str) -> dict[str, Any]:
        value = self._read(request_id, "request")
        if value is None:
            raise PackageChoiceError("PACKAGE_CHOICE_NOT_FOUND")
        body = {key: item for key, item in value.items() if key != "request_id"}
        if (value.get("version") not in RESULT_VERSIONS or value.get("request_id") != request_id
                or _hash(body) != request_id
                or ("requested_assemblies" in value) != (value["version"] == REQUEST_VERSION_V2)):
            raise PackageChoiceError("PACKAGE_CHOICE_ARTIFACT_CORRUPT")
        return value

    def status(self, request_id: str) -> dict[str, Any]:
        expected = RESULT_VERSIONS[self.request(request_id)["version"]]
        completed = self._read(request_id, "completed")
        if completed is not None:
            result = self._read(request_id, "result")
            if (result is None or set(completed) != {"request_id", "result_sha256"}
                    or completed["request_id"] != request_id or _hash(result) != completed["result_sha256"]
                    or result.get("version") != expected or result.get("request_id") != request_id):
                raise PackageChoiceError("PACKAGE_CHOICE_RESULT_CORRUPT")
            return {"request_id": request_id, "status": "completed",
                    "result_sha256": completed["result_sha256"], "result": result}
        failed = self._read(request_id, "failed")
        if failed is not None:
            code = failed.get("code")
            return {"request_id": request_id, "status": "failed",
                    "code": code if isinstance(code, str) and _CODE.fullmatch(code) else "PACKAGE_CHOICE_FAILED"}
        for artifact, state in (("worker-claim", "running"), ("start-unknown", "start_unknown"),
                                ("started", "started"), ("start-claim", "start_unknown")):
            if self._read(request_id, artifact) is not None:
                return {"request_id": request_id, "status": state}
        return {"request_id": request_id, "status": "requested"}

    def start(self, request_id: str) -> dict[str, Any]:
        existing = self.status(request_id)
        if existing["status"] != "requested":
            return existing
        if self.starter is None:
            raise PackageChoiceError("PACKAGE_CHOICE_STARTER_UNAVAILABLE")
        try:
            # A unique nonce: two callers writing the same marker can never both own the start.
            self._write(request_id, "start-claim", {"request_id": request_id, "claim_id": uuid.uuid4().hex})
        except BlobConflictError:
            return self.status(request_id)
        try:
            execution = self.starter.start({"REQUEST_ID": request_id})
            if not isinstance(execution, str) or not 0 < len(execution) <= 256:
                raise RuntimeError("execution name invalid")
            self._write(request_id, "started", {"request_id": request_id, "execution": execution})
        except Exception:
            # A transport failure may still have started the job: never start a second one.
            self._write(request_id, "start-unknown", {"request_id": request_id, "code": "START_UNKNOWN"})
        return self.status(request_id)

    def ensure(self, request: ProcessingRequest) -> dict[str, Any]:
        """API side: submit and start once; return the current status. No model calls."""
        record = self.submit(request)
        state = self.status(record["request_id"])
        if state["status"] == "requested":
            state = self.start(record["request_id"])
        return state

    def run(self, request_id: str, call: Caller) -> dict[str, Any]:
        """Worker side: download and verify the package files, then decide."""
        request = self.request(request_id)
        existing = self.status(request_id)
        if existing["status"] in {"completed", "failed", "running"}:
            return existing
        try:
            self._write(request_id, "worker-claim", {"request_id": request_id, "claim_id": uuid.uuid4().hex})
        except BlobConflictError:
            return self.status(request_id)
        try:
            import hashlib
            roots = []
            for item in request["package_files"]:
                try:
                    data = self.storage.download_source(item["blob_path"], item["size_bytes"])
                except Exception:
                    # Missing, unreadable or larger than recorded: never read past the recorded size.
                    raise PackageChoiceError("PACKAGE_FILE_UNAVAILABLE") from None
                if (type(data) is not bytes or len(data) != item["size_bytes"]
                        or hashlib.sha256(data).hexdigest() != item["sha256"]):
                    raise PackageChoiceError("PACKAGE_FILE_IDENTITY_MISMATCH")
                roots.append((item["file_name"], data))
            result = decide(request, roots, call)
            self._write(request_id, "result", result)
            self._write(request_id, "completed", {"request_id": request_id, "result_sha256": _hash(result)})
        except Exception as exc:
            self._write(request_id, "failed", {"request_id": request_id, "code": _stable_code(exc)})
        return self.status(request_id)


def apply_reopen(state: dict[str, Any], reopen: tuple[str, ...] | list[str] | None) -> dict[str, Any]:
    """Completed choice with the documents a person brought back processed again (plan 8.8 step 2).

    No model call; the stored result is not changed. Only a set-aside BOM can be brought back;
    files the survey called non-BOMs stay set aside. A reopened document joins its assembly's
    primary documents, so that assembly is reconciled (and any conflict reviewed) as before.
    """
    if reopen is None:
        return state
    if state.get("status") != "completed":
        raise PackageChoiceError("PACKAGE_CHOICE_REOPEN_INVALID")
    reopen = sorted(set(reopen))
    result = state["result"]
    documents = {item["document_id"]: item for item in result["documents"]}
    if not reopen or any(document_id not in documents or documents[document_id]["decision"] != "excluded"
                         or "bom" not in documents[document_id]["roles"] for document_id in reopen):
        raise PackageChoiceError("PACKAGE_CHOICE_REOPEN_INVALID")
    changed = [dict(item, decision="process", reason="reopened") if item["document_id"] in reopen else item
               for item in result["documents"]]
    selections = [dict(entry,
                       primary_document_ids=sorted(set(entry["primary_document_ids"])
                                                   | (set(entry["excluded_document_ids"]) & set(reopen))),
                       excluded_document_ids=sorted(set(entry["excluded_document_ids"]) - set(reopen)))
                  for entry in result["primary_selections"]]
    return {**state, "result": {**result, "documents": changed, "primary_selections": selections},
            "reopened_document_ids": reopen}


def choice_reference(state: dict[str, Any]) -> dict[str, Any]:
    """Binding recorded by plans and candidate requests; reopened ids only when present."""
    reference: dict[str, Any] = {"request_id": state["request_id"], "result_sha256": state["result_sha256"]}
    if state.get("reopened_document_ids") is not None:
        reference["reopened_document_ids"] = list(state["reopened_document_ids"])
    return reference


def bound_state(storage: Any, reference: Any) -> dict[str, Any]:
    """Re-read a recorded binding and re-apply its reopen; refuses anything that no longer matches."""
    if (not isinstance(reference, dict) or not set(reference) <= {"request_id", "result_sha256", "reopened_document_ids"}
            or not {"request_id", "result_sha256"} <= set(reference)):
        raise PackageChoiceError("PACKAGE_CHOICE_BINDING")
    state = PackageChoiceService(storage).status(str(reference["request_id"]))
    if state["status"] != "completed" or state["result_sha256"] != reference["result_sha256"]:
        raise PackageChoiceError("PACKAGE_CHOICE_BINDING")
    reopen = reference.get("reopened_document_ids")
    if reopen is not None and (not isinstance(reopen, list) or reopen != sorted(set(reopen))):
        raise PackageChoiceError("PACKAGE_CHOICE_BINDING")
    state = apply_reopen(state, reopen)
    if choice_reference(state) != reference:
        raise PackageChoiceError("PACKAGE_CHOICE_BINDING")
    return state


def excluded_assignments(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Package-plan assignments for the documents a completed choice set aside."""
    result = state["result"]
    reference = choice_reference(state)
    return [{"kind": "excluded", "document_ids": [item["document_id"]], "package_choice": reference,
             "reason": item["reason"], "roles": item["roles"], "assemblies": item["assemblies"]}
            for item in result["documents"] if item["decision"] == "excluded"]


def create_choice_caller() -> Caller:
    """The same Government model client as the parser author (lazy; offline imports stay offline)."""
    from .foundry_author import create_foundry_author
    from .package_survey_run import foundry_caller
    return foundry_caller(create_foundry_author().client)
