"""Validate quarantined parser candidates only through the isolated worker."""
from __future__ import annotations

import hashlib
import json
import math
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Callable

from .core import BlobConflictError, OUTPUT_CONTENT_TYPE, ProcessingError, _json_bytes
from .job_orchestrator import JobOrchestrator, WorkItem
from .remote import _validate_xlsx_bytes
from .master_registry import html_table_rows
from .candidates import unique_fixtures
from .arena_policy import is_arena_source, validate as validate_arena
from .semantic_facts import SemanticFactsError, facts_from_rows
from .document_roles import POLICY_VERSION as DOCUMENT_ROLE_POLICY, check_document_roles
from .candidate_authoring import ASSOCIATION_AUTHORING_VERSION

VALIDATION_VERSION = "candidate-validation.v3"
FAILURE_FEEDBACK_POLICY = "candidate-failure-feedback.v1"
MAX_METADATA_BYTES = 1_000_000
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA = re.compile(r"^[0-9a-f]{64}$")
_PLACEHOLDERS = {"UNKNOWN", "NA", "N/A", "TBD", "TBC", "NONE", "NULL", "MISSING", "PLACEHOLDER", "UNMAPPED"}


def _norm(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]+", "", str(value or "").upper())


def _semantic_output(output: bytes, source: bytes, subtype: str, *,
                     infer_document_assembly: bool = True) -> dict[str, Any]:
    import pandas as pd
    with tempfile.TemporaryDirectory(prefix="agentquote-semantic-") as directory:
        from pathlib import Path
        path = Path(directory) / "output.xlsx"
        path.write_bytes(output)
        frame = pd.read_excel(path, engine="openpyxl")
        # Preserve documentary cell types for role comparison only. Shared
        # semantic facts must keep the same loading rules as review consumers.
        role_frame = pd.read_excel(path, engine="openpyxl", dtype=object) if subtype == "xlsx" else None
    output_rows = len(frame)
    base = {"valid": False, "result": "semantic_output_invalid", "source_rows": 0, "output_rows": output_rows}
    if output_rows < 1:
        return {**base, "coverage_basis": "empty_output"}
    placeholders = {_norm(value) for value in _PLACEHOLDERS}
    identities = []
    for _, row in frame.iterrows():
        item, name = _norm(row.get("item_number")), _norm(row.get("item_name"))
        if not item or not name or item in placeholders or name in placeholders:
            return {**base, "coverage_basis": "identity"}
        quantity = row.get("quantity")
        document = _norm(row.get("item_category")) == "DOCUMENT"
        if not document and (isinstance(quantity, bool) or not isinstance(quantity, (int, float)) or not math.isfinite(float(quantity)) or quantity <= 0):
            return {**base, "coverage_basis": "quantity"}
        identities.append({item, name})
    source_rows, coverage_basis = 0, "not_applicable"
    if subtype == "html_table_xls":
        try:
            rows = [row for row in html_table_rows(source) if any(cell.strip() for cell in row)]
        except Exception:
            # Source is not actually HTML (e.g. a genuine binary OLE2 .xls
            # misclassified as html_table_xls). Fall back to output-only
            # semantic validation: identity and quantity checks already passed.
                parsed_rows = frame.to_dict(orient="records")
                facts = facts_from_rows(parsed_rows, infer_document_assembly=infer_document_assembly)
                return {"valid": True, "result": "valid", "source_rows": 0,
                    "output_rows": output_rows, "coverage_basis": "output_only",
                    "fingerprint": facts.fingerprint,
                    "assembly_identity": _assembly_dict(facts.assembly),
                    "mapping_required": facts.assembly is None,
                    "model_summary": facts.model_summary}
        header_index = next((i for i, row in enumerate(rows) if sum(bool(cell.strip()) for cell in row) >= 2), None)
        if header_index is None:
            return {**base, "coverage_basis": "source_header"}
        data_rows = rows[header_index + 1:]
        source_rows = len(data_rows)
        tokens = {_norm(cell) for row in data_rows for cell in row if _norm(cell) and _norm(cell) not in placeholders}
        if output_rows < max(1, math.ceil(source_rows * 0.5)) or not any(token in tokens for values in identities for token in values):
            return {**base, "source_rows": source_rows, "coverage_basis": "source_rows_and_identity"}
        coverage_basis = "source_rows_and_identity"
    parsed_rows = frame.to_dict(orient="records")
    document_roles = None
    if subtype == "xlsx":
        document_roles = check_document_roles(source, role_frame.to_dict(orient="records"))
        if not document_roles["valid"]:
            return {**base, "coverage_basis": "xlsx_document_roles",
                    "document_roles": document_roles}
    try:
        facts = facts_from_rows(parsed_rows, infer_document_assembly=infer_document_assembly)
    except SemanticFactsError:
        return {**base, "source_rows": source_rows,
                "coverage_basis": "assembly_identity_conflict"}
    return {"valid": True, "result": "valid", "source_rows": source_rows, "output_rows": output_rows,
            "coverage_basis": coverage_basis, "fingerprint": facts.fingerprint,
            "assembly_identity": _assembly_dict(facts.assembly),
            "mapping_required": facts.assembly is None,
            "model_summary": facts.model_summary,
            **({"document_roles": document_roles} if document_roles is not None else {})}


def _assembly_dict(assembly: Any) -> dict[str, str] | None:
    if assembly is None:
        return None
    name, revision = assembly.normalized()
    return {"name": name, "revision": revision}


def _semantic_feedback(semantic: dict[str, Any]) -> str:
    roles = semantic.get("document_roles")
    if isinstance(roles, dict) and roles.get("valid") is False:
        return (f"xlsx_document_roles:{roles['result']}; preserve each source document ID, revision, "
                "description and quantity in Document fields, not manufacturer/MPN fields; "
                "do not drop references or guess ambiguous roles")
    return f"semantic_output_invalid coverage_basis={semantic.get('coverage_basis', 'unknown')}"


def _source_subtype(request: dict[str, Any]) -> str:
    profile = request.get("profile", {})
    # Ordinary profiles omit their default subtype from serialized metadata.
    return str(profile.get("source_subtype") or
               ("xlsx" if str(profile.get("extension", "")).lower() in {"xlsx", ".xlsx"} else ""))


class CandidateValidationError(ValueError):
    """Safe, stable failure raised by candidate validation."""

    def __init__(self, code: str, classification: dict[str, str] | None = None):
        self.code = code
        self.classification = classification
        super().__init__(code)


@dataclass(frozen=True)
class CandidateValidationResult:
    request_id: str
    candidate_id: str
    validation_id: str
    evidence_path: str
    status: str


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _identifier(value: str, code: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise CandidateValidationError(code)
    return value


def _load_json(data: bytes, code: str) -> dict[str, Any]:
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise CandidateValidationError(code) from None
    if not isinstance(value, dict):
        raise CandidateValidationError(code)
    return value


class CandidateValidationService:
    """Submit one quarantined candidate to the isolated parser worker.

    This service never imports or executes candidate code and never writes an
    approved parser or registry record. State is derived from immutable events.
    """

    def __init__(self, request_id: str, candidate_id: str, storage: Any,
                 orchestrator: JobOrchestrator, *, timeout_seconds: int = 60,
                 poll_interval_seconds: int = 1, max_source_bytes: int = 50_000_000,
                 max_parser_bytes: int = 1_000_000, max_output_bytes: int = 50_000_000,
                 max_rows: int = 100_000, job_resource: str = "",
                 worker_image: str = "", clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep,
                 assembly_associations: dict[str, dict[str, Any]] | None = None,
                 serial_owner: str | None = None) -> None:
        self.request_id = _identifier(request_id, "INVALID_REQUEST_ID")
        self.candidate_id = _identifier(candidate_id, "INVALID_CANDIDATE_ID")
        limits = (timeout_seconds, poll_interval_seconds, max_source_bytes,
                  max_parser_bytes, max_output_bytes, max_rows)
        if any(type(value) is not int or value <= 0 for value in limits):
            raise ValueError("validation limits must be positive integers")
        self.storage, self.orchestrator = storage, orchestrator
        self.timeout_seconds, self.poll_interval_seconds = timeout_seconds, poll_interval_seconds
        self.max_source_bytes, self.max_parser_bytes = max_source_bytes, max_parser_bytes
        self.max_output_bytes, self.max_rows = max_output_bytes, max_rows
        self.job_resource, self.worker_image = job_resource, worker_image
        self.clock, self.sleep = clock, sleep
        self.assembly_associations = assembly_associations
        self.serial_owner = serial_owner
        self.validation_version = ("candidate-validation.v4" if assembly_associations is not None
                                   else VALIDATION_VERSION)

    def _association(self, source: bytes, evidence: dict[str, Any]) -> dict[str, Any] | None:
        if self.assembly_associations is None:
            return None
        from .assembly_association import verify_association
        try:
            result = verify_association(source, PurePosixPath(evidence["blob_path"]).suffix.lower(),
                                        self.assembly_associations[evidence["sha256"].lower()])
        except Exception:
            raise CandidateValidationError("ASSEMBLY_ASSOCIATION_INVALID") from None
        if result["has_blocking_findings"]:
            raise CandidateValidationError("ASSEMBLY_ASSOCIATION_REVIEW_REQUIRED")
        return result

    def validate(self) -> CandidateValidationResult:
        from .package_writer_gate import candidate_validation_writer
        from .serial_validation import transition, reserve
        from .author_budget import _membership
        from .semantic_repair import read_fence
        binding = _membership(self.storage, self.request_id)
        fence = read_fence(self.storage, binding[0]["plan_id"])[0] if binding else None
        if self.serial_owner is None and binding is not None and fence is None:
            # Supported direct validation must own the same slot too; it cannot
            # bypass a dispatcher reservation or overlap another enrolled worker.
            self.serial_owner = reserve(self.storage, self.request_id, self.candidate_id)
        if self.serial_owner is not None:
            transition(self.storage, self.request_id, self.candidate_id, self.serial_owner, "reserved", "running")
        try:
            with candidate_validation_writer(self.storage, self.request_id, self.candidate_id,
                                             clock=self.clock, sleep=self.sleep):
                result = self._validate_locked()
        except BaseException as exc:
            if (self.serial_owner is not None and getattr(exc, "feedback_recorded", False) is True
                    and getattr(exc, "code", None) in {"WORKER_PARSER_FAILED", "SCHEMA_INVALID", "SEMANTIC_INVALID"}):
                transition(self.storage, self.request_id, self.candidate_id, self.serial_owner, "running", "idle")
                self.serial_owner = None
            raise
        if self.serial_owner is not None:
            if result.status != "awaiting_approval":
                evidence = _load_json(self.storage.download_candidate(result.evidence_path, MAX_METADATA_BYTES),
                                      "VALIDATION_EVIDENCE_INVALID")
                failures = [item.get("failure") for item in evidence.get("fixtures", [])
                            if item.get("status") != "passed"]
                if not failures or any(not isinstance(item, dict) or item.get("repairable") is not True for item in failures):
                    return result  # Infrastructure/unknown outcome keeps persistent slot held.
            transition(self.storage, self.request_id, self.candidate_id, self.serial_owner, "running", "idle")
            self.serial_owner = None
        return result

    def _validate_locked(self) -> CandidateValidationResult:
        request = self._request()
        manifest, code = self._candidate(request)
        fixtures = unique_fixtures(request["source_evidence"])
        if self.assembly_associations is not None and set(self.assembly_associations) != {
                item["sha256"].lower() for item in fixtures}:
            raise CandidateValidationError("ASSEMBLY_ASSOCIATION_COVERAGE_INVALID")
        if len(fixtures) > 1 or len(fixtures[0].get("source_aliases", [])) > 1:
            return self._validate_group(request, manifest, code, fixtures)
        source_evidence = request["source_evidence"][0]
        source = self._source(source_evidence)
        association = self._association(source, source_evidence)
        validation_id = _sha256(_json_bytes({
            "version": self.validation_version,
            "document_role_policy": DOCUMENT_ROLE_POLICY,
            "request_id": self.request_id,
            "candidate_id": self.candidate_id,
            "code_sha256": manifest["code_sha256"],
            "fixture_sha256": source_evidence["sha256"].lower(),
            **({"assembly_association": association} if association is not None else {}),
        }))
        base = f"candidates/{self.request_id}/{self.candidate_id}/validation/{validation_id}"
        evidence_path = f"{base}/evidence.json"
        self._require_single_proof(evidence_path)
        existing = self._optional_candidate(evidence_path)
        if existing is not None:
            evidence = _load_json(existing, "VALIDATION_EVIDENCE_INVALID")
            return self._existing_result(evidence, validation_id, evidence_path)

        job_id = f"validate-{validation_id[:32]}"
        # The worker writes its result blob with If-None-Match:*, so the path
        # must be unique per attempt; a deterministic path would let the first
        # run's (possibly stale-format) result block every later re-run. The
        # prefix must still end with "/{job_id}" (WorkItem invariant).
        attempt_suffix = f"{int(time.time() * 1000)}"
        worker_prefix = f"{base}/worker/attempt-{attempt_suffix}/{job_id}"
        item = WorkItem(
            job_id, "boms", "parsers", "parsers", source_evidence["blob_path"],
            f"candidates/{self.request_id}/{self.candidate_id}/parse.py",
            source_evidence["sha256"].lower(), source_evidence["size_bytes"],
            manifest["code_sha256"], len(code), worker_prefix,
            f"input{PurePosixPath(source_evidence['blob_path']).suffix.lower()}",
            "output.xlsx", "result.json",
        )
        raw_result = None
        try:
            started = self.orchestrator.start(item)
            raw_result = self._poll(item)
            worker = self._verify_worker_result(raw_result, item)
            output = self.storage.download_candidate(item.output_blob, self.max_output_bytes)
        except CandidateValidationError as exc:
            classification = exc.classification
            exc.feedback_recorded = self._write_validation_failed_event(validation_id, item, exc.code, classification)
            raise
        except Exception:
            raise CandidateValidationError("WORKER_FAILED") from None

        schema_valid = True
        schema_code = "valid"
        try:
            _validate_xlsx_bytes(output, self.max_rows)
        except Exception:
            schema_valid = False
            schema_code = "canonical_schema_invalid"

        semantic = {"valid": False, "result": "semantic_output_invalid",
                    "source_rows": 0, "output_rows": 0,
                    "coverage_basis": "canonical_schema_invalid"}
        if schema_valid:
            try:
                semantic = _semantic_output(
                    output, source,
                    _source_subtype(request),
                    infer_document_assembly=association is None)
            except Exception:
                semantic = {"valid": False, "result": "semantic_output_invalid",
                            "source_rows": 0, "output_rows": 0,
                            "coverage_basis": "semantic_validation_failed"}
        validation_valid = schema_valid and semantic["valid"] is True
        arena_evidence = None
        if (schema_valid and str(request.get("profile", {}).get("source_subtype", "")) == "html_table_xls"
            and is_arena_source(source)):
            try:
                arena_evidence = validate_arena(
                    source, output, source_sha256=source_evidence["sha256"].lower(),
                    candidate_code_sha256=manifest["code_sha256"], output_sha256=_sha256(output))
            except Exception:
                arena_evidence = {"version": "arena-html-policy.v1", "valid": False, "reason": "policy_error"}
            validation_valid = validation_valid and arena_evidence.get("valid") is True
            if arena_evidence.get("valid") is not True:
                semantic = {"valid": False, "result": "semantic_output_invalid",
                            "source_rows": semantic.get("source_rows", 0),
                            "output_rows": semantic.get("output_rows", 0),
                            "coverage_basis": f"arena_policy:{arena_evidence.get('reason', 'invalid')}"}
        resulting_status = "awaiting_approval" if validation_valid else "generated_unvalidated"
        evidence = {
            "version": self.validation_version,
            "document_role_policy": DOCUMENT_ROLE_POLICY,
            "failure_feedback_policy": FAILURE_FEEDBACK_POLICY,
            "validation_id": validation_id,
            "request_id": self.request_id,
            "candidate_id": self.candidate_id,
            "prior_status": "generated_unvalidated",
            "status": resulting_status,
            "candidate": {"blob_path": item.parser_blob, "sha256": manifest["code_sha256"],
                          "size_bytes": len(code)},
            "fixtures": [{"document_id": source_evidence["document_id"],
                           "blob_path": source_evidence["blob_path"],
                           "sha256": source_evidence["sha256"].lower(),
                           "size_bytes": source_evidence["size_bytes"]}],
            "output": {"blob_path": item.output_blob, "sha256": _sha256(output),
                       "size_bytes": len(output), "content_type": OUTPUT_CONTENT_TYPE},
            "schema": {"valid": schema_valid, "result": schema_code},
            "semantic": semantic,
            "worker": {"contract_version": worker["contract_version"],
                       "job_resource": self.job_resource,
                       "execution_name": started.execution_name,
                       "image_reference": self.worker_image},
        }
        if association is not None:
            evidence["assembly_association"] = association
        if arena_evidence is not None:
            evidence["arena_policy"] = arena_evidence
        try:
            self.storage.upload_candidate_immutable(
                evidence_path, _json_bytes(evidence), "application/json")
            if validation_valid:
                event_path = (f"candidates/events/{self.request_id}/{self.candidate_id}/"
                              f"validated-{validation_id}.json")
                event = {"version": self.validation_version, "event": "candidate.validated",
                         "request_id": self.request_id, "candidate_id": self.candidate_id,
                         "validation_id": validation_id, "prior_status": "generated_unvalidated",
                         "status": "awaiting_approval", "evidence_path": evidence_path,
                         "code_sha256": manifest["code_sha256"]}
                self.storage.upload_candidate_immutable(event_path, _json_bytes(event), "application/json")
        except BlobConflictError:
            raise CandidateValidationError("VALIDATION_CONFLICT") from None
        if not schema_valid:
            recorded = self._write_validation_failed_event(
                validation_id, item, "SCHEMA_INVALID",
                {"error_type": "SchemaError", "error_message": "canonical_schema_invalid"})
            failure = CandidateValidationError("SCHEMA_INVALID")
            failure.feedback_recorded = recorded
            raise failure
        if not semantic["valid"]:
            recorded = self._write_validation_failed_event(
                validation_id, item, "SEMANTIC_INVALID",
                {"error_type": "SemanticError",
                 "error_message": _semantic_feedback(semantic)})
            failure = CandidateValidationError("SEMANTIC_INVALID")
            failure.feedback_recorded = recorded
            raise failure
        return CandidateValidationResult(self.request_id, self.candidate_id, validation_id,
                                         evidence_path, resulting_status)

    def _validate_group(self, request: dict[str, Any], manifest: dict[str, Any], code: bytes,
                        fixtures: tuple[dict[str, Any], ...]) -> CandidateValidationResult:
        """Validate every unique byte fixture and persist one aggregate record."""
        if self.assembly_associations is not None:
            for fixture in fixtures:
                self._association(self._source(fixture), fixture)
        validation_id = _sha256(_json_bytes({"version": self.validation_version, "document_role_policy": DOCUMENT_ROLE_POLICY, "request_id": self.request_id,
                                             "candidate_id": self.candidate_id, "code_sha256": manifest["code_sha256"],
                                                            "fixtures": [item["sha256"].lower() for item in fixtures],
                                                            **({"assembly_associations": self.assembly_associations}
                                                                if self.assembly_associations is not None else {})}))
        base = f"candidates/{self.request_id}/{self.candidate_id}/validation/{validation_id}"
        evidence_path = f"{base}/evidence.json"
        self._require_single_proof(evidence_path)
        existing = self._optional_candidate(evidence_path)
        if existing is not None:
            return self._existing_result(_load_json(existing, "VALIDATION_EVIDENCE_INVALID"), validation_id, evidence_path)
        parsed: list[dict[str, Any]] = []
        all_valid = True
        for index, source_evidence in enumerate(fixtures):
            source = self._source(source_evidence)
            association = self._association(source, source_evidence)
            fixture_id = _sha256(_json_bytes({"validation_id": validation_id, "fixture": source_evidence["sha256"].lower()}))
            job_id = f"validate-{fixture_id[:32]}"
            item = WorkItem(job_id, "boms", "parsers", "parsers", source_evidence["blob_path"],
                            f"candidates/{self.request_id}/{self.candidate_id}/parse.py",
                            source_evidence["sha256"].lower(), source_evidence["size_bytes"],
                            manifest["code_sha256"], len(code), f"{base}/worker/attempt-{int(time.time()*1000)}-{index}/{job_id}",
                            f"input{PurePosixPath(source_evidence['blob_path']).suffix.lower()}", "output.xlsx", "result.json")
            record = {"fixture_sha256": source_evidence["sha256"].lower(), "document_id": source_evidence["document_id"],
                      "source_aliases": source_evidence.get("source_aliases", []), "status": "failed"}
            try:
                started = self.orchestrator.start(item)
                raw = self._poll(item)
                worker = self._verify_worker_result(raw, item)
                output = self.storage.download_candidate(item.output_blob, self.max_output_bytes)
                schema_valid = True
                try:
                    _validate_xlsx_bytes(output, self.max_rows)
                except Exception:
                    schema_valid = False
                semantic = _semantic_output(output, source, _source_subtype(request),
                    infer_document_assembly=association is None) if schema_valid else {"valid": False, "result": "schema_invalid"}
                policy = None
                if schema_valid and str(request.get("profile", {}).get("source_subtype", "")) == "html_table_xls" and is_arena_source(source):
                    policy = validate_arena(source, output, source_sha256=source_evidence["sha256"].lower(),
                                            candidate_code_sha256=manifest["code_sha256"], output_sha256=_sha256(output))
                valid = schema_valid and semantic.get("valid") is True and (policy is None or policy.get("valid") is True)
                all_valid = all_valid and valid
                record.update({"status": "passed" if valid else "failed", "output": {"blob_path": item.output_blob,
                    "sha256": _sha256(output), "size_bytes": len(output), "content_type": OUTPUT_CONTENT_TYPE},
                    "schema": {"valid": schema_valid}, "semantic": semantic, "worker": {"execution_name": started.execution_name},
                    "arena_policy": policy})
                if association is not None:
                    record["assembly_association"] = association
                if not valid:
                    record["failure"] = {"code": "SCHEMA_INVALID" if not schema_valid else "SEMANTIC_INVALID",
                                         "repairable": True,
                                         "classification": {"error_type": "ValidationError",
                                             "error_message": "output schema invalid" if not schema_valid else _semantic_feedback(semantic)}}
            except Exception as exc:
                all_valid = False
                code_value = exc.code if isinstance(exc, CandidateValidationError) else "WORKER_FAILED"
                record["failure"] = {"code": code_value, "repairable": code_value == "WORKER_PARSER_FAILED"}
                if isinstance(exc, CandidateValidationError) and exc.classification is not None:
                    record["failure"]["classification"] = exc.classification
            parsed.append(record)
        evidence = {"version": self.validation_version, "validation_id": validation_id, "request_id": self.request_id,
                    "document_role_policy": DOCUMENT_ROLE_POLICY,
                    "failure_feedback_policy": FAILURE_FEEDBACK_POLICY,
                    "candidate_id": self.candidate_id, "prior_status": "generated_unvalidated",
                    "status": "awaiting_approval" if all_valid else "generated_unvalidated",
                    "candidate": {"blob_path": f"candidates/{self.request_id}/{self.candidate_id}/parse.py",
                                   "sha256": manifest["code_sha256"], "size_bytes": len(code)},
                    "fixtures": [dict(item) for item in parsed],
                    "fixture_count": len(parsed), "complete": len(parsed) == len(fixtures),
                    "policy_versions": ["arena-html-policy.v1"], "outputs": parsed}
        try:
            self.storage.upload_candidate_immutable(evidence_path, _json_bytes(evidence), "application/json")
            if all_valid:
                event_path = f"candidates/events/{self.request_id}/{self.candidate_id}/validated-{validation_id}.json"
                self.storage.upload_candidate_immutable(event_path, _json_bytes({"version": self.validation_version,
                    "event": "candidate.validated", "request_id": self.request_id, "candidate_id": self.candidate_id,
                    "validation_id": validation_id, "status": "awaiting_approval", "evidence_path": evidence_path}), "application/json")
            else:
                self._write_group_failed_event(validation_id, evidence_path, evidence)
        except BlobConflictError:
            raise CandidateValidationError("VALIDATION_CONFLICT") from None
        return CandidateValidationResult(self.request_id, self.candidate_id, validation_id, evidence_path,
                                         "awaiting_approval" if all_valid else "generated_unvalidated")

    def _require_single_proof(self, evidence_path: str) -> None:
        """Hold before dispatch when new policy would create ambiguous proof history."""
        root = f"candidates/{self.request_id}/{self.candidate_id}/validation/"
        paths = self.storage.list_candidate_paths(root, 100)
        if len(paths) >= 100 or any(not path.startswith(root) or ".." in path.split("/") for path in paths):
            raise CandidateValidationError("VALIDATION_HISTORY_INVALID")
        if any(path.endswith("/evidence.json") and path != evidence_path for path in paths):
            # Never overwrite or reinterpret old proof; a fresh author policy
            # produces a distinct candidate suitable for ordinary validation.
            raise CandidateValidationError("VALIDATION_POLICY_REAUTHOR_REQUIRED")

    def _request(self) -> dict[str, Any]:
        try:
            value = _load_json(self.storage.download_registry(
                f"candidates/requests/{self.request_id}.json", MAX_METADATA_BYTES), "REQUEST_INVALID")
        except CandidateValidationError:
            raise
        except Exception:
            raise CandidateValidationError("REQUEST_UNAVAILABLE") from None
        evidence = value.get("source_evidence")
        if (value.get("request_id") != self.request_id or value.get("status") != "requested"
                or not isinstance(evidence, list) or not evidence
                or any(not isinstance(item, dict) for item in evidence)):
            raise CandidateValidationError("REQUEST_INVALID")
        for item in evidence:
            if (not isinstance(item.get("document_id"), str)
                    or not isinstance(item.get("blob_path"), str)
                    or type(item.get("size_bytes")) is not int or item["size_bytes"] < 0
                    or not isinstance(item.get("sha256"), str)
                    or not _SHA.fullmatch(item["sha256"].lower())):
                raise CandidateValidationError("REQUEST_INVALID")
        return value

    def _candidate(self, request: dict[str, Any]) -> tuple[dict[str, Any], bytes]:
        base = f"candidates/{self.request_id}/{self.candidate_id}"
        try:
            manifest = _load_json(self.storage.download_candidate(
                f"{base}/candidate-manifest.json", MAX_METADATA_BYTES), "CANDIDATE_INVALID")
            code = self.storage.download_candidate(f"{base}/parse.py", self.max_parser_bytes)
        except CandidateValidationError:
            raise
        except Exception:
            raise CandidateValidationError("CANDIDATE_UNAVAILABLE") from None
        code_hash = _sha256(code)
        signature = request.get("profile", {}).get("signature_hash", request.get("profile_hash"))
        identity = {"request_id": self.request_id, "code_sha256": code_hash,
                    "signature": signature}
        # Bind every authoring policy revision into the candidate identity.
        version = manifest.get("version")
        if isinstance(version, str) and version.startswith("candidate-authoring."):
            identity["authoring_version"] = version
        if version in {"candidate-authoring.v6", ASSOCIATION_AUTHORING_VERSION}:
            ref = manifest.get("assembly_associations")
            if (not isinstance(ref, dict) or set(ref) != {"blob_path", "sha256", "size_bytes"}
                    or ref["blob_path"] != f"{base}/assembly-associations.json"
                    or type(ref["size_bytes"]) is not int or not 0 < ref["size_bytes"] <= MAX_METADATA_BYTES
                    or not isinstance(ref["sha256"], str) or not _SHA.fullmatch(ref["sha256"])):
                raise CandidateValidationError("ASSOCIATION_ARTIFACT_INVALID")
            raw = self.storage.download_candidate(ref["blob_path"], MAX_METADATA_BYTES)
            if len(raw) != ref["size_bytes"] or _sha256(raw) != ref["sha256"]:
                raise CandidateValidationError("ASSOCIATION_ARTIFACT_INTEGRITY")
            associations = _load_json(raw, "ASSOCIATION_ARTIFACT_INVALID")
            if self.assembly_associations is not None and self.assembly_associations != associations:
                raise CandidateValidationError("ASSOCIATION_POLICY_MISMATCH")
            self.assembly_associations = associations
            self.validation_version = "candidate-validation.v4"
            identity["association_sha256"] = ref["sha256"]
        expected_id = _sha256(_json_bytes(identity))
        if (manifest.get("status") != "generated_unvalidated"
                or manifest.get("request_id") != self.request_id
                or manifest.get("candidate_id") != self.candidate_id
                or manifest.get("code_sha256") != code_hash
                or expected_id != self.candidate_id):
            raise CandidateValidationError("CANDIDATE_INTEGRITY")
        return manifest, code

    def _source(self, evidence: dict[str, Any]) -> bytes:
        try:
            source = self.storage.download_source(evidence["blob_path"], self.max_source_bytes)
        except Exception:
            raise CandidateValidationError("SOURCE_UNAVAILABLE") from None
        if len(source) != evidence["size_bytes"] or _sha256(source) != evidence["sha256"].lower():
            raise CandidateValidationError("SOURCE_INTEGRITY")
        return source

    def _poll(self, item: WorkItem) -> bytes:
        deadline = self.clock() + self.timeout_seconds
        while self.clock() <= deadline:
            value = self._optional_candidate(item.result_blob)
            if value is not None:
                return value
            self.sleep(self.poll_interval_seconds)
        raise CandidateValidationError("WORKER_TIMEOUT")

    def _optional_candidate(self, path: str) -> bytes | None:
        try:
            return self.storage.download_candidate(path, self.max_output_bytes)
        except Exception as exc:
            if exc.__class__.__name__ == "BlobNotFoundError":
                return None
            raise

    def _verify_worker_result(self, raw: bytes, item: WorkItem) -> dict[str, Any]:
        value = _load_json(raw, "WORKER_RESULT_INVALID")
        expected_contract = "remote-worker.v2" if item.interpretation_name else "remote-worker.v1"
        if (value.get("contract_version") != expected_contract
            or value.get("job") != item.job_id or value.get("status") not in {"completed", "failed"}):
            raise CandidateValidationError("WORKER_RESULT_INVALID")
        for section, expected_hash, expected_size in (
                ("input", item.input_sha256, item.input_size),
                ("parser", item.parser_sha256, item.parser_size)):
            item_evidence = value.get(section)
            if (not isinstance(item_evidence, dict) or item_evidence.get("verified") is not True
                    or str(item_evidence.get("sha256", "")).lower() != expected_hash
                    or item_evidence.get("size") != expected_size):
                raise CandidateValidationError("WORKER_EVIDENCE_INVALID")
        if value["status"] == "failed":
            classification = self._extract_failure_classification(raw)
            # The existing transfer wrapper labels parser exits transfer_failed.
            # Recognize only its fixed prefix after pinned inputs were verified.
            parser_failed = (value.get("failure") == "transfer_failed" and classification is not None
                             and classification["error_type"] == "RuntimeError"
                                and (classification["error_message"] == "parser failed"
                                    or classification["error_message"].startswith("parser failed | ")))
            raise CandidateValidationError("WORKER_PARSER_FAILED" if parser_failed else "WORKER_EXECUTION_FAILED",
                                           classification)
        output = value.get("output")
        if (not isinstance(output, dict) or output.get("content_type") != OUTPUT_CONTENT_TYPE
                or type(output.get("size")) is not int or not 0 < output["size"] <= self.max_output_bytes
                or not isinstance(output.get("sha256"), str) or not _SHA.fullmatch(output["sha256"].lower())):
            raise CandidateValidationError("WORKER_OUTPUT_INVALID")
        try:
            data = self.storage.download_candidate(item.output_blob, self.max_output_bytes)
        except Exception:
            raise CandidateValidationError("WORKER_OUTPUT_MISSING") from None
        if len(data) != output["size"] or _sha256(data) != output["sha256"].lower():
            raise CandidateValidationError("WORKER_OUTPUT_INTEGRITY")
        if item.interpretation_name:
            interpretation = value.get("interpretation")
            if (not isinstance(interpretation, dict) or interpretation.get("content_type") != "application/json"
                    or type(interpretation.get("size")) is not int or not 0 < interpretation["size"] <= 1024 * 1024
                    or not isinstance(interpretation.get("sha256"), str)
                    or not _SHA.fullmatch(interpretation["sha256"])):
                raise CandidateValidationError("WORKER_INTERPRETATION_INVALID")
            try:
                sidecar = self.storage.download_candidate(item.interpretation_blob, 1024 * 1024)
                if len(sidecar) != interpretation["size"] or _sha256(sidecar) != interpretation["sha256"]:
                    raise CandidateValidationError("WORKER_INTERPRETATION_INTEGRITY")
                source = self.storage.download_source(item.source_blob, self.max_source_bytes)
                if len(source) != item.input_size or _sha256(source) != item.input_sha256:
                    raise CandidateValidationError("WORKER_SOURCE_INTEGRITY")
                from .interpretation_coverage import verify_coverage
                verified = verify_coverage(source, PurePosixPath(item.input_name).suffix, data, sidecar)
            except CandidateValidationError:
                raise
            except Exception:
                raise CandidateValidationError("WORKER_INTERPRETATION_VERIFICATION_FAILED") from None
            if interpretation.get("verification") != verified:
                raise CandidateValidationError("WORKER_INTERPRETATION_CLAIM_MISMATCH")
            if verified["has_blocking_findings"]:
                raise CandidateValidationError("WORKER_INTERPRETATION_REVIEW_REQUIRED")
            # Return independently bound evidence, never trust an arbitrary
            # worker-supplied path or promote provenance to semantic authority.
            value["interpretation"] = {"blob_path": item.interpretation_blob,
                                       "sha256": _sha256(sidecar), "size": len(sidecar),
                                       "content_type": "application/json", "verification": verified}
        return value

    def _existing_result(self, evidence: dict[str, Any], validation_id: str,
                         evidence_path: str) -> CandidateValidationResult:
        if (evidence.get("version") != self.validation_version
                or evidence.get("validation_id") != validation_id
                or evidence.get("request_id") != self.request_id
                or evidence.get("candidate_id") != self.candidate_id):
            raise CandidateValidationError("VALIDATION_EVIDENCE_INVALID")
        status = evidence.get("status")
        fixtures = evidence.get("fixtures")
        if isinstance(fixtures, list) and fixtures and any("fixture_sha256" in item for item in fixtures if isinstance(item, dict)):
            if (evidence.get("complete") is not True or len(fixtures) != evidence.get("fixture_count")
                    or any(not isinstance(item, dict) for item in fixtures)):
                raise CandidateValidationError("VALIDATION_EVIDENCE_INVALID")
            passed = all(item.get("status") == "passed" for item in fixtures)
            expected_status = "awaiting_approval" if passed else "generated_unvalidated"
            if status != expected_status:
                raise CandidateValidationError("VALIDATION_EVIDENCE_INVALID")
            if (status == "generated_unvalidated"
                    and evidence.get("failure_feedback_policy") == FAILURE_FEEDBACK_POLICY):
                self._write_group_failed_event(validation_id, evidence_path, evidence)
            return CandidateValidationResult(self.request_id, self.candidate_id, validation_id,
                                             evidence_path, status)
        semantic = evidence.get("semantic")
        if (status != "awaiting_approval" or not isinstance(semantic, dict)
            or semantic.get("valid") is not True or semantic.get("result") != "valid"):
            raise CandidateValidationError("SCHEMA_INVALID")
        return CandidateValidationResult(self.request_id, self.candidate_id, validation_id,
                                         evidence_path, status)

    @staticmethod
    def _extract_failure_classification(raw_result: bytes | None) -> dict[str, str] | None:
        """Pull the bounded failure classification from the worker result, if present."""
        if raw_result is None:
            return None
        try:
            value = json.loads(raw_result.decode("utf-8"))
        except Exception:
            return None
        if not isinstance(value, dict):
            return None
        classification = value.get("failure_classification")
        if (isinstance(classification, dict) and isinstance(classification.get("error_type"), str)
            and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,99}", classification["error_type"])
            and isinstance(classification.get("error_message"), str)):
            message = classification["error_message"][:4000]
            message = re.sub(r"(?:https?://|[A-Za-z]:[\\/]|\\\\|/(?:tmp|home|Users|var|private)/)[^\s\"<>]*", "<path>", message)
            # Arbitrary stderr is untrusted. Withhold the entire message rather
            # than partially redact credentials with spaces or quoted values.
            if re.search(r"(?i)\b(?:authorization|bearer|password|secret|token|api[_-]?key|sig)[\"']?\s*(?::|=|\s)", message):
                message = "sensitive feedback withheld"
            message = " ".join(message.split())
            return {"error_type": classification["error_type"], "error_message": message[:500]}
        return None

    def _write_group_failed_event(self, validation_id: str, evidence_path: str,
                                  evidence: dict[str, Any]) -> None:
        failures = [{"fixture_sha256": fixture["fixture_sha256"], **fixture["failure"]}
                for fixture in evidence["fixtures"] if "failure" in fixture]
        repairable = bool(failures) and all(failure["repairable"] for failure in failures)
        messages = sorted({failure.get("classification", {}).get("error_message", failure["code"])
                   for failure in failures})
        event = {"version": self.validation_version, "event": "candidate.validation_failed",
             "failure_feedback_policy": FAILURE_FEEDBACK_POLICY, "request_id": self.request_id,
             "candidate_id": self.candidate_id, "validation_id": validation_id,
             "evidence_path": evidence_path, "evidence_sha256": _sha256(_json_bytes(evidence)),
             "error_code": "GROUP_VALIDATION_FAILED", "failure": "GroupedValidationError",
             "failure_message": " | ".join(messages)[:500], "repairable": repairable,
             "fixtures": failures}
        path = f"candidates/events/{self.request_id}/{self.candidate_id}/validation-failed-{validation_id}.json"
        self.storage.upload_candidate_immutable(path, _json_bytes(event), "application/json")

    def _write_validation_failed_event(self, validation_id: str, item: WorkItem,
                                       error_code: str,
                                       classification: dict[str, str] | None) -> bool:
        """Persist an immutable validation-failed event for the refinement loop.

        Best effort: a failure to write the event must not mask the original
        validation error, so all exceptions here are swallowed.
        """
        try:
            event = {
                "version": VALIDATION_VERSION,
                "failure_feedback_policy": FAILURE_FEEDBACK_POLICY,
                "repairable": error_code in {"WORKER_PARSER_FAILED", "SCHEMA_INVALID", "SEMANTIC_INVALID"},
                "event": "candidate.validation_failed",
                "request_id": self.request_id,
                "candidate_id": self.candidate_id,
                "validation_id": validation_id,
                "error_code": error_code,
                "failure": (classification or {}).get("error_type"),
                "failure_message": (classification or {}).get("error_message"),
                "worker_job": item.job_id,
            }
            path = (f"candidates/events/{self.request_id}/{self.candidate_id}/"
                    f"validation-failed-{validation_id}.json")
            self.storage.upload_candidate_immutable(path, _json_bytes(event), "application/json")
            return True
        except Exception:
            return False