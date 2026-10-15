"""Operator-submitted parser candidates (Nick 2026-10-14, option 1).

A person may supply a parser for an existing candidate request instead of waiting for the model.
The parser enters through the normal authoring boundary (CandidateAuthoringService with a fixed
author): the same code policy, the same immutable parse.py + manifest + generated event. Then one
immutable operator selection record names it, so the status projection (candidate_status) chooses it
over the budget lineage or the authoring rank. From there everything is unchanged: status polling
starts the normal validator job, the package review reads the same status, and a person's approval
is still required. Nothing here approves, promotes, or skips a check.

Selection record: candidates/operator-selections/<request_id>/<candidate_id>.json (registry
container, write-once). Fields: version, request_id, candidate_id, code_sha256, actor, reason,
selected_time, selection_hash. Exactly one valid record selects; more than one is ambiguous and
fails closed; a record whose candidate is missing or whose code hash differs fails closed.

offline_validate runs a parser through the real offline validation path (child-process worker,
schema, semantic and row-accounting checks) against one local source, with in-memory storage only.
Run: python -m pytest tests/test_operator_candidate.py
"""
from __future__ import annotations

import hashlib
import io
import json
import re
from datetime import datetime, timezone
from typing import Any, Callable

from .core import BlobConflictError

SELECTION_VERSION = "operator-candidate-selection.v1"
SELECTION_ROOT = "candidates/operator-selections"
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
MAX_REASON_CHARS = 500


class OperatorCandidateError(ValueError):
    """Stable, fail-closed refusal; the message is a code."""


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


class FixedCodeAuthor:
    """A ModelAuthor that returns one operator-supplied parser and never anything else.

    A policy refusal is not corrected: the operator's code is used as given or not at all.
    """
    def __init__(self, code: str):
        if not isinstance(code, str) or not code.strip():
            raise OperatorCandidateError("OPERATOR_PARSER_EMPTY")
        self.code = code

    def generate(self, prompt: Any, source_bytes: bytes, prior_code: str | None = None,
                 failed_rule_id: str | None = None) -> str:
        if prior_code is not None:
            # The authoring boundary asks for one correction after a policy refusal; an operator
            # parser is never rewritten, so the refusal stands.
            raise TypeError("operator parsers are not corrected")
        return self.code

    def generate_bundle(self, prompt: Any, sources: Any) -> Any:
        raise OperatorCandidateError("OPERATOR_PARSER_BUNDLE_UNSUPPORTED")


def selection_path(request_id: str, candidate_id: str) -> str:
    for value in (request_id, candidate_id):
        if not isinstance(value, str) or not _ID.fullmatch(value):
            raise OperatorCandidateError("OPERATOR_SELECTION_ID_INVALID")
    return f"{SELECTION_ROOT}/{request_id}/{candidate_id}.json"


def selection_record(*, request_id: str, candidate_id: str, code_sha256: str, actor: str, reason: str,
                     clock: Callable[[], datetime] | None = None) -> dict[str, Any]:
    """The immutable record that selects one candidate for its request."""
    selection_path(request_id, candidate_id)
    if not isinstance(code_sha256, str) or not _HASH.fullmatch(code_sha256):
        raise OperatorCandidateError("OPERATOR_SELECTION_HASH_INVALID")
    if not isinstance(actor, str) or not _ID.fullmatch(actor):
        raise OperatorCandidateError("OPERATOR_SELECTION_ACTOR_INVALID")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > MAX_REASON_CHARS:
        raise OperatorCandidateError("OPERATOR_SELECTION_REASON_INVALID")
    now = (clock or (lambda: datetime.now(timezone.utc)))()
    now = now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now.astimezone(timezone.utc)
    body = {"version": SELECTION_VERSION, "request_id": request_id, "candidate_id": candidate_id,
            "code_sha256": code_sha256, "actor": actor, "reason": reason.strip(),
            "selected_time": now.isoformat().replace("+00:00", "Z")}
    return {**body, "selection_hash": _hash(body)}


def _valid_record(value: Any, request_id: str, candidate_id: str) -> bool:
    expected = {"version", "request_id", "candidate_id", "code_sha256", "actor", "reason",
                "selected_time", "selection_hash"}
    if not isinstance(value, dict) or set(value) != expected:
        return False
    body = {key: item for key, item in value.items() if key != "selection_hash"}
    return (value["version"] == SELECTION_VERSION and value["request_id"] == request_id
            and value["candidate_id"] == candidate_id and value["selection_hash"] == _hash(body)
            and isinstance(value["code_sha256"], str) and bool(_HASH.fullmatch(value["code_sha256"])))


def operator_selection(storage: Any, request_id: str, manifests: dict[str, dict[str, Any]],
                       max_results: int = 100) -> str | None:
    """The candidate an operator selected for this request, or None when there is no record.

    Fails closed (OperatorCandidateError) on several records, an unreadable or tampered record, a
    record naming a candidate that does not exist, or a code hash that differs from the manifest.
    """
    root = f"{SELECTION_ROOT}/{request_id}/"
    paths = storage.list_registry_paths(root, max_results)
    if not paths:
        return None
    if len(paths) != 1:
        raise OperatorCandidateError("OPERATOR_SELECTION_AMBIGUOUS")
    path = paths[0]
    candidate_id = path[len(root):-len(".json")] if path.startswith(root) and path.endswith(".json") else ""
    if not _ID.fullmatch(candidate_id):
        raise OperatorCandidateError("OPERATOR_SELECTION_INVALID")
    try:
        value = json.loads(storage.download_registry(path, 100_000).decode("utf-8"))
    except Exception as exc:
        raise OperatorCandidateError("OPERATOR_SELECTION_INVALID") from exc
    if not _valid_record(value, request_id, candidate_id):
        raise OperatorCandidateError("OPERATOR_SELECTION_INVALID")
    manifest = manifests.get(candidate_id)
    if manifest is None or manifest.get("code_sha256") != value["code_sha256"]:
        raise OperatorCandidateError("OPERATOR_SELECTION_CANDIDATE_MISMATCH")
    return candidate_id


def submit(storage: Any, request_id: str, code: str):
    """Write the operator parser as a candidate through the normal authoring boundary."""
    from .candidate_authoring import CandidateAuthoringService
    return CandidateAuthoringService(request_id, storage, FixedCodeAuthor(code)).author()


def select(storage: Any, record: dict[str, Any]) -> str:
    """Write the selection record once; an identical replay is accepted, anything else refused."""
    path = selection_path(record["request_id"], record["candidate_id"])
    manifest_path = f"candidates/{record['request_id']}/{record['candidate_id']}/candidate-manifest.json"
    manifest = json.loads(storage.download_candidate(manifest_path, 1_000_000).decode("utf-8"))
    if manifest.get("code_sha256") != record["code_sha256"] or manifest.get("request_id") != record["request_id"]:
        raise OperatorCandidateError("OPERATOR_SELECTION_CANDIDATE_MISMATCH")
    existing = storage.list_registry_paths(f"{SELECTION_ROOT}/{record['request_id']}/", 100)
    if existing and existing != [path]:
        raise OperatorCandidateError("OPERATOR_SELECTION_AMBIGUOUS")
    try:
        storage.upload_registry_immutable(path, _json_bytes(record), "application/json")
    except BlobConflictError:
        prior = json.loads(storage.download_registry(path, 100_000).decode("utf-8"))
        if {key: prior.get(key) for key in record if key not in {"selected_time", "selection_hash"}} != {
                key: value for key, value in record.items() if key not in {"selected_time", "selection_hash"}}:
            raise OperatorCandidateError("OPERATOR_SELECTION_CONFLICT") from None
    return path


def apply(storage: Any, request_id: str, code: str, *, actor: str, reason: str, hold_writer: bool = True,
          clock: Callable[[], datetime] | None = None):
    """Submit the parser and select it, as one operator action; returns (authoring result, record).

    For a request enrolled in a package plan the plan's writer is held (the same short writer the
    author and recovery paths take), so this cannot race a package approval, and a plan that is
    already approved is refused. hold_writer=False only for a dry run on a recording view.
    """
    from contextlib import nullcontext
    from .author_budget import _membership
    from .package_writer_gate import candidate_writer
    member = _membership(storage, request_id)
    if member is not None and storage.list_registry_paths(f"packages/{member[0]['plan_id']}/approvals/", 100):
        raise OperatorCandidateError("OPERATOR_PLAN_ALREADY_APPROVED")
    with (candidate_writer(storage, request_id, "recovery") if hold_writer else nullcontext()):
        authored = submit(storage, request_id, code)
        record = selection_record(request_id=request_id, candidate_id=authored.candidate_id,
                                  code_sha256=hashlib.sha256(code.encode("utf-8")).hexdigest(),
                                  actor=actor, reason=reason, clock=clock)
        select(storage, record)
    return authored, record


class OfflineWorker:
    """The real child-process parser worker against in-memory storage (no Azure)."""
    def __init__(self, store: Any, timeout_seconds: int = 30):
        self.store, self.timeout_seconds = store, timeout_seconds

    def start(self, item: Any):
        from worker.runtime import Limits, run_job
        from .core import OUTPUT_CONTENT_TYPE
        from .job_orchestrator import StartResult
        ran = run_job(job=item.job_id, parser_bytes=self.store.candidates[item.parser_blob],
                      input_bytes=self.store.sources[item.source_blob], input_name=item.input_name,
                      output_name=item.output_name, output_policy="opaque", return_output=True,
                      limits=Limits(timeout_seconds=self.timeout_seconds), row_map_name=item.row_map_name)
        result, code, output = ran[:3]
        if code != 0:
            self.store.candidates[item.result_blob] = _json_bytes(
                {**result, "contract_version": "remote-worker.v3"})
            raise OperatorCandidateError(f"PARSER_FAILED: {result.get('failure')}: {str(result.get('stderr_tail'))[-600:]}")
        self.store.candidates[item.output_blob] = output
        self.store.candidates[item.row_map_blob] = ran[3]
        self.store.candidates[item.result_blob] = _json_bytes({
            **result, "contract_version": "remote-worker.v3",
            "input": {**result["input"], "verified": True}, "parser": {**result["parser"], "verified": True},
            "output": {**result["output"], "content_type": OUTPUT_CONTENT_TYPE},
            "row_map": {**result["row_map"], "content_type": "application/json"}})
        return StartResult(item.job_id, "offline-child-process", datetime.now(timezone.utc))


def offline_validate(source: bytes, extension: str, code: str,
                     request_id: str = "offline-operator-check") -> dict[str, Any]:
    """Validate one parser on one local source through the real offline path; returns a summary."""
    from openpyxl import load_workbook
    from . import InMemoryStorage
    from .candidate_validation import CandidateValidationError, CandidateValidationService
    store = InMemoryStorage()
    path = f"offline/rfq-00000000-0000-4000-8000-000000000000/package-1/original-boms/bom{extension}"
    store.sources[path] = source
    store.registry[f"candidates/requests/{request_id}.json"] = _json_bytes({
        "version": "candidate-request.v2", "request_id": request_id, "status": "requested",
        "parser_key": "offline", "customer_key": "offline", "profile_hash": "0" * 64,
        "profile": {"extension": extension},
        "source_evidence": [{"document_id": "doc-1", "blob_path": path, "size_bytes": len(source),
                             "sha256": hashlib.sha256(source).hexdigest()}]})
    authored = submit(store, request_id, code)
    service = CandidateValidationService(request_id, authored.candidate_id, store, OfflineWorker(store))
    summary: dict[str, Any] = {"candidate_id": authored.candidate_id}
    try:
        summary["state"] = service.validate().status
    except CandidateValidationError as exc:
        summary["state"] = f"failed:{exc.code}"
        events = [key for key in store.candidates
                  if key.startswith(f"candidates/events/{request_id}/{authored.candidate_id}/validation-failed-")]
        if events:
            event = json.loads(store.candidates[events[0]])
            summary["failure_message"] = str(event.get("failure_message", ""))[:2000]
            accounting = event.get("row_accounting")
            if isinstance(accounting, dict):
                summary["row_accounting"] = {key: accounting.get(key) for key in (
                    "failure_codes", "failed_rows", "unmapped_columns", "pattern_findings",
                    "field_mismatches", "unlinked_fields") if key in accounting}
    outputs = [key for key in store.candidates
               if key.startswith(f"candidates/{request_id}/{authored.candidate_id}/") and key.endswith("output.xlsx")]
    if outputs:
        book = load_workbook(io.BytesIO(store.candidates[outputs[0]]), read_only=True)
        values = list(book.active.values)
        book.close()
        rows = [dict(zip(values[0], row)) for row in values[1:]]
        parts = [row for row in rows if row["item_category"] == "Part"]
        summary.update({
            "lines": len(rows), "parts": len(parts),
            "documents": sum(1 for row in rows if row["item_category"] == "Document"),
            "total_part_quantity": sum(row["quantity"] for row in parts if isinstance(row["quantity"], (int, float))),
            "parts_without_quantity": sum(1 for row in parts if row["quantity"] is None),
            "alternate_pairs": sum(1 for row in rows for key in row
                                   if key.startswith("manufacturer_item_number_") and key != "manufacturer_item_number_1"
                                   and row[key] is not None),
            "lines_with_notes": sum(1 for row in rows if row.get("bom_notes")),
            "derived_board": sum(1 for row in rows if str(row.get("bom_notes") or "").find("bare board") >= 0),
            "rows": rows})
    return summary
