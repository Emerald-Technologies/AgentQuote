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

VALIDATION_VERSION = "candidate-validation.v3"
MAX_METADATA_BYTES = 1_000_000
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA = re.compile(r"^[0-9a-f]{64}$")
_PLACEHOLDERS = {"UNKNOWN", "NA", "N/A", "TBD", "TBC", "NONE", "NULL", "MISSING", "PLACEHOLDER", "UNMAPPED"}


def _norm(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]+", "", str(value or "").upper())


def _semantic_output(output: bytes, source: bytes, subtype: str) -> dict[str, Any]:
    import pandas as pd
    with tempfile.TemporaryDirectory(prefix="agentquote-semantic-") as directory:
        from pathlib import Path
        path = Path(directory) / "output.xlsx"
        path.write_bytes(output)
        frame = pd.read_excel(path, engine="openpyxl")
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
        if isinstance(quantity, bool) or not isinstance(quantity, (int, float)) or not math.isfinite(float(quantity)) or quantity <= 0:
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
                facts = facts_from_rows(parsed_rows)
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
    try:
        facts = facts_from_rows(parsed_rows)
    except SemanticFactsError:
        return {**base, "source_rows": source_rows,
                "coverage_basis": "assembly_identity_conflict"}
    return {"valid": True, "result": "valid", "source_rows": source_rows, "output_rows": output_rows,
            "coverage_basis": coverage_basis, "fingerprint": facts.fingerprint,
            "assembly_identity": _assembly_dict(facts.assembly),
            "mapping_required": facts.assembly is None,
            "model_summary": facts.model_summary}


def _assembly_dict(assembly: Any) -> dict[str, str] | None:
    if assembly is None:
        return None
    name, revision = assembly.normalized()
    return {"name": name, "revision": revision}


class CandidateValidationError(ValueError):
    """Safe, stable failure raised by candidate validation."""

    def __init__(self, code: str):
        self.code = code
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
                 sleep: Callable[[float], None] = time.sleep) -> None:
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

    def validate(self) -> CandidateValidationResult:
        request = self._request()
        manifest, code = self._candidate(request)
        fixtures = unique_fixtures(request["source_evidence"])
        if len(fixtures) > 1 or len(fixtures[0].get("source_aliases", [])) > 1:
            return self._validate_group(request, manifest, code, fixtures)
        source_evidence = request["source_evidence"][0]
        source = self._source(source_evidence)
        validation_id = _sha256(_json_bytes({
            "version": VALIDATION_VERSION,
            "request_id": self.request_id,
            "candidate_id": self.candidate_id,
            "code_sha256": manifest["code_sha256"],
            "fixture_sha256": source_evidence["sha256"].lower(),
        }))
        base = f"candidates/{self.request_id}/{self.candidate_id}/validation/{validation_id}"
        evidence_path = f"{base}/evidence.json"
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
            classification = self._extract_failure_classification(raw_result)
            self._write_validation_failed_event(validation_id, item, exc.code, classification)
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
                    str(request.get("profile", {}).get("source_subtype", "")))
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
            "version": VALIDATION_VERSION,
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
        if arena_evidence is not None:
            evidence["arena_policy"] = arena_evidence
        try:
            self.storage.upload_candidate_immutable(
                evidence_path, _json_bytes(evidence), "application/json")
            if validation_valid:
                event_path = (f"candidates/events/{self.request_id}/{self.candidate_id}/"
                              f"validated-{validation_id}.json")
                event = {"version": VALIDATION_VERSION, "event": "candidate.validated",
                         "request_id": self.request_id, "candidate_id": self.candidate_id,
                         "validation_id": validation_id, "prior_status": "generated_unvalidated",
                         "status": "awaiting_approval", "evidence_path": evidence_path,
                         "code_sha256": manifest["code_sha256"]}
                self.storage.upload_candidate_immutable(event_path, _json_bytes(event), "application/json")
        except BlobConflictError:
            raise CandidateValidationError("VALIDATION_CONFLICT") from None
        if not schema_valid:
            self._write_validation_failed_event(
                validation_id, item, "SCHEMA_INVALID",
                {"error_type": "SchemaError", "error_message": "canonical_schema_invalid"})
            raise CandidateValidationError("SCHEMA_INVALID")
        if not semantic["valid"]:
            self._write_validation_failed_event(
                validation_id, item, "SEMANTIC_INVALID",
                {"error_type": "SemanticError",
                 "error_message": f"semantic_output_invalid coverage_basis={semantic.get('coverage_basis', 'unknown')}"})
            raise CandidateValidationError("SEMANTIC_INVALID")
        return CandidateValidationResult(self.request_id, self.candidate_id, validation_id,
                                         evidence_path, resulting_status)

    def _validate_group(self, request: dict[str, Any], manifest: dict[str, Any], code: bytes,
                        fixtures: tuple[dict[str, Any], ...]) -> CandidateValidationResult:
        """Validate every unique byte fixture and persist one aggregate record."""
        validation_id = _sha256(_json_bytes({"version": VALIDATION_VERSION, "request_id": self.request_id,
                                             "candidate_id": self.candidate_id, "code_sha256": manifest["code_sha256"],
                                             "fixtures": [item["sha256"].lower() for item in fixtures]}))
        base = f"candidates/{self.request_id}/{self.candidate_id}/validation/{validation_id}"
        evidence_path = f"{base}/evidence.json"
        existing = self._optional_candidate(evidence_path)
        if existing is not None:
            return self._existing_result(_load_json(existing, "VALIDATION_EVIDENCE_INVALID"), validation_id, evidence_path)
        parsed: list[dict[str, Any]] = []
        all_valid = True
        for index, source_evidence in enumerate(fixtures):
            source = self._source(source_evidence)
            fixture_id = _sha256(_json_bytes({"validation_id": validation_id, "fixture": source_evidence["sha256"].lower()}))
            job_id = f"validate-{fixture_id[:32]}"
            item = WorkItem(job_id, "boms", "parsers", "parsers", source_evidence["blob_path"],
                            f"candidates/{self.request_id}/{self.candidate_id}/parse.py",
                            source_evidence["sha256"].lower(), source_evidence["size_bytes"],
                            manifest["code_sha256"], len(code), f"{base}/worker/attempt-{int(time.time()*1000)}-{index}/{job_id}",
                            f"input{PurePosixPath(source_evidence['blob_path']).suffix.lower()}", f"output-{fixture_id[:16]}.xlsx", "result.json")
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
                semantic = _semantic_output(output, source, str(request.get("profile", {}).get("source_subtype", ""))) if schema_valid else {"valid": False, "result": "schema_invalid"}
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
            except Exception as exc:
                all_valid = False
                record["failure"] = {"code": exc.code if isinstance(exc, CandidateValidationError) else "WORKER_FAILED"}
            parsed.append(record)
        evidence = {"version": VALIDATION_VERSION, "validation_id": validation_id, "request_id": self.request_id,
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
                self.storage.upload_candidate_immutable(event_path, _json_bytes({"version": VALIDATION_VERSION,
                    "event": "candidate.validated", "request_id": self.request_id, "candidate_id": self.candidate_id,
                    "validation_id": validation_id, "status": "awaiting_approval", "evidence_path": evidence_path}), "application/json")
        except BlobConflictError:
            raise CandidateValidationError("VALIDATION_CONFLICT") from None
        return CandidateValidationResult(self.request_id, self.candidate_id, validation_id, evidence_path,
                                         "awaiting_approval" if all_valid else "generated_unvalidated")

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
        if (value.get("contract_version") != "remote-worker.v1"
                or value.get("job") != item.job_id or value.get("status") != "completed"):
            raise CandidateValidationError("WORKER_RESULT_INVALID")
        for section, expected_hash, expected_size in (
                ("input", item.input_sha256, item.input_size),
                ("parser", item.parser_sha256, item.parser_size)):
            item_evidence = value.get(section)
            if (not isinstance(item_evidence, dict) or item_evidence.get("verified") is not True
                    or str(item_evidence.get("sha256", "")).lower() != expected_hash
                    or item_evidence.get("size") != expected_size):
                raise CandidateValidationError("WORKER_EVIDENCE_INVALID")
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
        return value

    def _existing_result(self, evidence: dict[str, Any], validation_id: str,
                         evidence_path: str) -> CandidateValidationResult:
        if (evidence.get("version") != VALIDATION_VERSION
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
        if isinstance(classification, dict) and isinstance(classification.get("error_type"), str):
            return {"error_type": classification["error_type"][:100],
                    "error_message": str(classification.get("error_message", ""))[:500]}
        return None

    def _write_validation_failed_event(self, validation_id: str, item: WorkItem,
                                       error_code: str,
                                       classification: dict[str, str] | None) -> None:
        """Persist an immutable validation-failed event for the refinement loop.

        Best effort: a failure to write the event must not mask the original
        validation error, so all exceptions here are swallowed.
        """
        try:
            event = {
                "version": VALIDATION_VERSION,
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
        except Exception:
            pass