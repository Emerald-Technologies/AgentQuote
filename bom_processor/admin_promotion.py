"""Trusted API-layer promotion decisions; candidate code is only copied, never run."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Callable

from .core import BlobConflictError, OUTPUT_CONTENT_TYPE, _json_bytes
from .master_registry import ApprovalRecord, ApprovedVariant, RegistryError, VariantSignature, _hash
from .remote import _validate_xlsx_bytes
from .candidate_review import CandidateReviewError, CandidateReviewService

MAX_METADATA_BYTES = 1_000_000
MAX_REASON_CHARS = 2_000
MAX_TICKET_CHARS = 256


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read(storage: Any, path: str, candidate: bool = False) -> dict[str, Any]:
    data = (storage.download_candidate if candidate else storage.download_registry)(path, MAX_METADATA_BYTES)
    try:
        value = json.loads(data.decode("utf-8"))
    except Exception as exc:
        raise RegistryError("PROMOTION_EVIDENCE_INVALID") from exc
    if not isinstance(value, dict):
        raise RegistryError("PROMOTION_EVIDENCE_INVALID")
    return value


def _write(storage: Any, path: str, value: dict[str, Any]) -> None:
    storage.upload_registry_immutable(path, _json_bytes(value), "application/json")


class AdminDecisionService:
    """Validate immutable evidence and promote one candidate with CAS retries."""
    def __init__(self, storage: Any, *, clock: Callable[[], datetime] | None = None,
                 max_retries: int = 5, max_output_bytes: int = 50_000_000,
                 max_parser_bytes: int = 1_000_000, max_rows: int = 100_000):
        if type(max_retries) is not int or not 1 <= max_retries <= 20:
            raise ValueError("invalid retry limit")
        self.storage, self.clock, self.max_retries = storage, clock or (lambda: datetime.now(timezone.utc)), max_retries
        self.max_output_bytes, self.max_parser_bytes, self.max_rows = max_output_bytes, max_parser_bytes, max_rows

    def decide(self, *, request_id: str, candidate_id: str, validation_id: str,
               decision: str, approver: Any, reason: str, ticket: str,
               variant_id: str | None = None, version: str | None = None,
               review_manifest_sha256: str | None = None,
               fixture_selections: dict[str, str] | None = None) -> dict[str, Any]:
        from .package_writer_gate import candidate_writer
        with candidate_writer(self.storage, request_id, "approval"):
            return self._decide_locked(request_id=request_id, candidate_id=candidate_id,
                validation_id=validation_id, decision=decision, approver=approver,
                reason=reason, ticket=ticket, variant_id=variant_id, version=version,
                review_manifest_sha256=review_manifest_sha256, fixture_selections=fixture_selections)

    def _decide_locked(self, *, request_id: str, candidate_id: str, validation_id: str,
               decision: str, approver: Any, reason: str, ticket: str,
               variant_id: str | None = None, version: str | None = None,
               review_manifest_sha256: str | None = None,
               fixture_selections: dict[str, str] | None = None) -> dict[str, Any]:
        if not all(isinstance(x, str) and x and len(x) <= 128 and all(c.isalnum() or c in "._-" for c in x)
                   for x in (request_id, candidate_id, validation_id)):
            raise RegistryError("INVALID_DECISION_ID")
        if (decision not in {"approve", "reject"} or not isinstance(reason, str)
            or len(reason) > MAX_REASON_CHARS or not isinstance(ticket, str)
            or len(ticket) > MAX_TICKET_CHARS
            or (decision == "reject" and (not reason.strip() or not ticket.strip()))):
            raise RegistryError("INVALID_DECISION")
        if not isinstance(approver, dict) or not isinstance(approver.get("object_id"), str) or not approver["object_id"].strip():
            raise RegistryError("INVALID_APPROVER")
        if decision == "approve" and (not isinstance(variant_id, str) or not isinstance(version, str)):
            raise RegistryError("INVALID_VARIANT")
        if decision == "reject" and (variant_id is not None or version is not None):
            raise RegistryError("INVALID_VARIANT")
        if (review_manifest_sha256 is not None and
                (not isinstance(review_manifest_sha256, str) or len(review_manifest_sha256) != 64
                 or any(char not in "0123456789abcdefABCDEF" for char in review_manifest_sha256))):
            raise RegistryError("INVALID_DECISION")
        if fixture_selections is not None and (not isinstance(fixture_selections, dict)
                or any(not isinstance(key, str) or not isinstance(value, str)
                       for key, value in fixture_selections.items())):
            raise RegistryError("INVALID_DECISION")
        request = _read(self.storage, f"candidates/requests/{request_id}.json")
        if request.get("request_id") != request_id or request.get("status") != "requested": raise RegistryError("REQUEST_BINDING")
        base = f"candidates/{request_id}/{candidate_id}"
        manifest = _read(self.storage, f"{base}/candidate-manifest.json", True)
        code = self.storage.download_candidate(f"{base}/parse.py", self.max_parser_bytes)
        if manifest.get("request_id") != request_id or manifest.get("candidate_id") != candidate_id or manifest.get("status") != "generated_unvalidated" or manifest.get("code_sha256") != _sha(code):
            raise RegistryError("CANDIDATE_BINDING")
        evidence_path = f"{base}/validation/{validation_id}/evidence.json"
        evidence = _read(self.storage, evidence_path, True)
        grouped_v3 = (evidence.get("version") == "candidate-validation.v4" or
                 (evidence.get("version") == "candidate-validation.v3"
                  and isinstance(evidence.get("fixtures"), list)
                  and any(isinstance(item, dict) and "fixture_sha256" in item
                      for item in evidence["fixtures"])))
        singleton_valid = (evidence.get("schema", {}).get("valid") is True)
        grouped_valid = (evidence.get("complete") is True
                 and evidence.get("fixture_count") == len(evidence.get("fixtures", []))
                 and all(isinstance(item, dict) and item.get("status") == "passed"
                     for item in evidence.get("fixtures", [])))
        if (evidence.get("version") == "candidate-validation.v4"
                and len(evidence.get("fixtures", [])) == 1
                and "fixture_sha256" not in evidence["fixtures"][0]):
            grouped_valid = singleton_valid
        if (evidence.get("request_id") != request_id or evidence.get("candidate_id") != candidate_id
                or evidence.get("validation_id") != validation_id or evidence.get("status") != "awaiting_approval"
                or evidence.get("candidate", {}).get("sha256") != _sha(code)
            or not (grouped_valid if grouped_v3 else singleton_valid)):
            raise RegistryError("VALIDATION_BINDING")
        try:
            review = CandidateReviewService(self.storage).get_v3(request_id, candidate_id, validation_id)
        except CandidateReviewError as exc:
            raise RegistryError("VALIDATION_BINDING") from exc
        if not review.get("validation_complete"):
            raise RegistryError("VALIDATION_BINDING")
        evidence_manifest_hash = review.get("evidence", {}).get("manifest_sha256")
        if not isinstance(evidence_manifest_hash, str):
            evidence_manifest_hash = _hash(evidence.get("fixtures", []))
        if decision == "approve" and grouped_v3:
            if review_manifest_sha256 is None or review_manifest_sha256.lower() != evidence_manifest_hash:
                raise RegistryError("REVIEW_MANIFEST_BINDING")
            supplied = fixture_selections or {}
            required_groups = {group["group_id"]: group for group in review.get("groups", [])
                               if group.get("has_conflicts") is True}
            if set(supplied) != set(required_groups):
                raise RegistryError("FIXTURE_SELECTION_INVALID")
            for group_id, selected_hash in supplied.items():
                displayed = {item.get("fixture_sha256") for item in required_groups[group_id].get("fixtures", [])}
                if selected_hash not in displayed:
                    raise RegistryError("FIXTURE_SELECTION_INVALID")
            if any(group.get("mapping_required") is True for group in review.get("groups", [])):
                raise RegistryError("MAPPING_REQUIRED")
        elif fixture_selections:
            raise RegistryError("FIXTURE_SELECTION_INVALID")
        if request.get("profile", {}).get("source_subtype") == "html_table_xls" and not grouped_v3:
            semantic = evidence.get("semantic")
            if (not isinstance(semantic, dict) or semantic.get("valid") is not True
                    or semantic.get("result") != "valid"
                    or not all(isinstance(semantic.get(key), int) for key in ("source_rows", "output_rows"))
                    or not isinstance(semantic.get("coverage_basis"), str)):
                raise RegistryError("VALIDATION_BINDING")
        event_path = f"candidates/events/{request_id}/{candidate_id}/validated-{validation_id}.json"
        event = _read(self.storage, event_path, True)
        if event.get("validation_id") != validation_id or event.get("status") != "awaiting_approval": raise RegistryError("VALIDATION_EVENT_INVALID")
        if not grouped_v3:
            output = evidence.get("output", {})
            output_bytes = self.storage.download_candidate(output.get("blob_path", ""), self.max_output_bytes)
            if _sha(output_bytes) != output.get("sha256") or len(output_bytes) != output.get("size_bytes") or output.get("content_type") != OUTPUT_CONTENT_TYPE:
                raise RegistryError("OUTPUT_BINDING")
            try: _validate_xlsx_bytes(output_bytes, self.max_rows)
            except Exception as exc: raise RegistryError("OUTPUT_SCHEMA_INVALID") from exc
        signature = self._signature(request)
        if decision == "approve":
            if variant_id is None or version is None or not all(c.isalnum() or c in "._-" for c in variant_id + version): raise RegistryError("INVALID_VARIANT")
        reason, ticket = reason.strip(), ticket.strip()
        now = self.clock(); now = now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now.astimezone(timezone.utc)
        timestamp = now.isoformat().replace("+00:00", "Z")
        identity = {"version":"admin-decision.v1", "request_id":request_id, "candidate_id":candidate_id, "validation_id":validation_id, "decision":decision, "variant_id":variant_id, "variant_version":version, "approver":approver["object_id"], "reason":reason, "ticket":ticket}
        if grouped_v3:
            identity["review_manifest_sha256"] = evidence_manifest_hash
            identity["fixture_selections"] = dict(sorted((fixture_selections or {}).items()))
        approval_id = _hash(identity)
        decision_path = f"candidates/decisions/{request_id}/{candidate_id}/{validation_id}.json"
        record = {**identity, "approved_time": timestamp, "decision_id": approval_id}
        try:
            _write(self.storage, decision_path, record)
        except BlobConflictError:
            existing = _read(self.storage, decision_path)
            # Time is assigned only on the first immutable decision. Replays
            # compare the complete caller-controlled identity, then resume any
            # interrupted idempotent promotion using the original timestamp.
            if ({key: existing.get(key) for key in identity} != identity
                    or existing.get("decision_id") != approval_id
                    or not isinstance(existing.get("approved_time"), str)):
                raise RegistryError("DECISION_CONFLICT")
            record = existing
            timestamp = existing["approved_time"]
        if decision == "reject":
            _write(self.storage, f"candidates/events/{request_id}/{candidate_id}/decision-{approval_id}.json", {**record, "event":"candidate.rejected"})
            return record
        approval = ApprovalRecord(approval_id, candidate_id, request["parser_key"], request["customer_key"], variant_id, version,
                                  _hash(manifest), _sha(code), signature.signature_hash, signature.rule_hash,
                      evidence_manifest_hash, approver["object_id"], timestamp)
        parser_key = request["parser_key"]
        prefix = f"approved/{parser_key}/{variant_id}/{version}"
        for path, data in ((f"{prefix}/parse.py", code), (f"{prefix}/signature.json", _json_bytes(signature.to_dict())), (f"{prefix}/approval.json", _json_bytes(approval.to_dict()))):
            self.storage.upload_registry_immutable(path, data, "text/x-python" if path.endswith(".py") else "application/json")
        variant = ApprovedVariant(parser_key, variant_id, version, f"{prefix}/parse.py", _sha(code), signature.signature_hash, signature.rule_hash, approval_id)
        generation = self._publish(parser_key, variant, signature, record)
        return {**record, "parser": {"key": parser_key, "blob_path": variant.code_path,
                "sha256": variant.code_sha256, "approval_id": approval_id,
                "registry_generation": generation, "rule_hash": signature.rule_hash,
                "signature_hash": signature.signature_hash, "selection_mode": "registry",
                "variant_id": variant.variant_id, "version": variant.version}}

    @staticmethod
    def _signature(request: dict[str, Any]) -> VariantSignature:
        return VariantSignature.from_profile(request.get("profile", {}))

    def _publish(self, parser_key: str, variant: ApprovedVariant, signature: VariantSignature, record: dict[str, Any]) -> int:
        path = f"registry/{parser_key}/master.json"
        for _ in range(self.max_retries):
            try:
                raw, etag = self.storage.download_registry_with_etag(path, MAX_METADATA_BYTES)
                master = json.loads(raw.decode()); variants = master.get("variants", [])
            except Exception as exc:
                if exc.__class__.__name__ != "BlobNotFoundError": raise
                master, variants, etag = {"schema":"master-registry.v1", "parser_key":parser_key, "generation":0, "variants":[]}, [], None
            expected = {**variant.to_dict(),
                        "signature_path": f"approved/{parser_key}/{variant.variant_id}/{variant.version}/signature.json",
                        "approval_path": f"approved/{parser_key}/{variant.variant_id}/{variant.version}/approval.json"}
            if any(item.get("variant_id") == variant.variant_id and item.get("version") == variant.version for item in variants):
                if next(item for item in variants if item.get("variant_id") == variant.variant_id and item.get("version") == variant.version) != expected: raise RegistryError("REGISTRY_CONFLICT")
                return int(master["generation"])
            variants.append(expected)
            master = {"schema":"master-registry.v1", "parser_key":parser_key, "generation":master["generation"] + 1, "variants":variants}
            master_bytes = _json_bytes(master)
            snapshot_hash = _sha(master_bytes)
            self.storage.upload_registry_immutable(
                f"registry/{parser_key}/generations/{master['generation']}-{snapshot_hash}.json",
                master_bytes, "application/json")
            promotion_event = {"version": "registry-promotion.v1", "event": "candidate.promoted",
                               "decision_id": record["decision_id"], "candidate_id": record["candidate_id"],
                               "validation_id": record["validation_id"], "parser_key": parser_key,
                               "variant_id": variant.variant_id, "variant_version": variant.version,
                               "generation": master["generation"], "master_sha256": snapshot_hash}
            self.storage.upload_registry_immutable(
                f"registry/{parser_key}/events/{record['decision_id']}-{master['generation']}.json",
                _json_bytes(promotion_event), "application/json")
            try:
                self.storage.upload_registry_if_match(path, master_bytes, "application/json", etag)
                return int(master["generation"])
            except BlobConflictError: continue
            except Exception:
                # A transient storage failure must not leave the registry pointer
                # behind the immutable generation snapshot and promotion event that
                # were already published. Re-read and retry the conditional pointer
                # update within the same bounded retry budget.
                continue
        raise RegistryError("REGISTRY_CONFLICT")
