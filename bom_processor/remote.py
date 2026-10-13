"""Remote Container Apps Job BOM processor.

The service remains synchronous: jobs are started one at a time and their
immutable worker artifacts are verified before the normal QuickQuote output is
published.  This module deliberately depends only on the existing storage and
orchestration protocols, making it straightforward to test without Azure.
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import PurePosixPath
from datetime import datetime, timezone
from typing import Any, Callable

from .core import (CONTRACT_VERSION, OUTPUT_CONTENT_TYPE, BlobConflictError,
                   BlobNotFoundError, ProcessingError, ProcessingRequest,
                   Storage, _json_bytes, _sha256, _validate_xlsx)
from .job_orchestrator import JobOrchestrator, WorkItem
from .blob_registry import BlobRegistryReader
from .master_registry import RegistryError
from .master_registry import profile_bytes
from .candidates import (AggregateCandidateRequiredError, CandidateRequiredError,
                         persist_candidate)
from .row_accounting import (MAX_ROW_MAP_BYTES, RESULT_VERSION as ROW_ACCOUNTING_RESULT_VERSION,
                             ROW_MAP_NAME, RowMapError, verify_row_map)


class RemoteBomProcessor:
    def __init__(self, storage: Storage, orchestrator: JobOrchestrator, *, timeout_seconds=60,
                 poll_interval_seconds=1, max_source_bytes=50_000_000,
                 max_total_source_bytes=100_000_000, max_parser_bytes=1_000_000,
                 max_output_bytes=50_000_000, max_rows=100_000,
                 registry_reader: BlobRegistryReader | None = None,
                 candidate_clock=None,
                 author_budget_enabled: bool = False,
                 author_budget_quote_ids: frozenset[str] = frozenset(),
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep,
                 package_choice: Any = None) -> None:
        limits = (timeout_seconds, poll_interval_seconds, max_source_bytes, max_total_source_bytes,
                  max_parser_bytes, max_output_bytes, max_rows)
        if any(type(value) is not int or value <= 0 for value in limits):
            raise ValueError("processing limits must be positive integers")
        self.storage, self.orchestrator = storage, orchestrator
        self.timeout_seconds, self.poll_interval_seconds = timeout_seconds, poll_interval_seconds
        self.max_source_bytes, self.max_total_source_bytes = max_source_bytes, max_total_source_bytes
        self.max_parser_bytes, self.max_output_bytes, self.max_rows = max_parser_bytes, max_output_bytes, max_rows
        self.clock, self.sleep = clock, sleep
        self.candidate_clock = candidate_clock
        self.author_budget_enabled = author_budget_enabled
        from .quote_budget_scope import parse_quote_scope
        if type(author_budget_enabled) is not bool or not isinstance(author_budget_quote_ids, frozenset):
            raise ValueError("invalid author budget scope")
        self.author_budget_quote_ids = parse_quote_scope(",".join(sorted(author_budget_quote_ids)))
        reader = getattr(storage, "download_registry", None)
        self.registry_reader = registry_reader or (BlobRegistryReader(reader, max_bytes=max_parser_bytes) if reader else None)
        # bom_package_choice.PackageChoiceService with a job starter; used only for requests that
        # carry package_files (plan 8.8 step 2). Model calls run in that job, never here.
        self.package_choice = package_choice

    def _package_choice(self, request: ProcessingRequest) -> dict[str, Any] | None:
        """Completed choice for a request with package_files, else None. Raises while pending."""
        if request.package_files is None:
            return None
        if self.package_choice is None:
            raise ProcessingError("package choice unavailable")
        from .bom_package_choice import PackageChoiceError, PackageChoicePendingError, apply_reopen
        try:
            state = self.package_choice.ensure(request)
        except PackageChoiceError as exc:
            raise ProcessingError("package choice invalid") from exc
        if state["status"] != "completed":
            raise PackageChoicePendingError(state["request_id"], state["status"], state.get("code"))
        try:
            # Documents a person brought back are processed again; no new model call.
            return apply_reopen(state, request.package_choice_reopen)
        except PackageChoiceError as exc:
            raise ProcessingError("package choice reopen invalid") from exc

    def _budget_enabled(self, request: ProcessingRequest) -> bool:
        return (request.package_files is not None or self.author_budget_enabled
                or request.rfq_uuid.lower() in self.author_budget_quote_ids)

    def process(self, request: ProcessingRequest) -> dict[str, Any]:
        request.validate()
        if any(d.size_bytes > self.max_source_bytes for d in request.documents):
            raise ProcessingError("source exceeds limit")
        if sum(d.size_bytes for d in request.documents) > self.max_total_source_bytes:
            raise ProcessingError("request exceeds aggregate source limit")
        prefix = f"{request.customer_key}/rfq-{request.rfq_uuid}/package-{request.package_version}/processed/{request.job_id}"
        manifest_path, claim_path = f"{prefix}/manifest.json", f"{prefix}/claim.json"
        fingerprint = request.fingerprint()
        choice = self._package_choice(request)
        self._choice = choice
        # Without a package choice every request document is processed, exactly as before.
        kept = request.documents if choice is None else tuple(
            document for document in request.documents if document.document_id in {
                item["document_id"] for item in choice["result"]["documents"] if item["decision"] == "process"})
        if request.force_candidate:
            verified_sources = {}
            for document in kept:
                source = self.storage.download_source(document.blob_path, self.max_source_bytes)
                if len(source) != document.size_bytes or _sha256(source).lower() != document.sha256.lower():
                    raise ProcessingError("source integrity check failed")
                verified_sources[document.document_id] = source
            self._require_candidate(request, verified_sources, ProcessingError("replacement requested"), kept)
        existing = self.storage.download_output(manifest_path, self.max_output_bytes)
        if existing is not None:
            return self._validate_manifest(existing, request, fingerprint)
        verified_sources: dict[str, bytes] = {}
        for document in kept:
            source = self.storage.download_source(document.blob_path, self.max_source_bytes)
            if len(source) != document.size_bytes or _sha256(source).lower() != document.sha256.lower():
                raise ProcessingError("source integrity check failed")
            verified_sources[document.document_id] = source
        selections: dict[str, Any] = {}
        legacy_parser: bytes | None = None
        pending: list[CandidateRequiredError] = []
        profiles: dict[str, Any] = {}
        try:
            if self.registry_reader is None:
                raise RegistryError("REGISTRY_ABSENT")
            for document in kept:
                try:
                    current = self.registry_reader.select(
                        request.parser_key, request.customer_key,
                        verified_sources[document.document_id],
                        PurePosixPath(document.blob_path).suffix,
                    )
                except RegistryError as item_error:
                    if item_error.code not in {"PARSER_FORMAT_NOT_RECOGNIZED"}:
                        raise
                    profile = profile_bytes(verified_sources[document.document_id],
                                            PurePosixPath(document.blob_path).suffix)
                    profiles.setdefault(profile.profile_hash, (profile, []))[1].append(document)
                    continue
                selections[document.document_id] = current
        except RegistryError as exc:
            if exc.code == "REGISTRY_ABSENT":
                try:
                    legacy_parser = self.storage.download_parser(request.parser_key, self.max_parser_bytes)
                except BlobNotFoundError:
                    pass
                if legacy_parser is not None:
                    pass
                else:
                    exc = RegistryError("PARSER_FORMAT_NOT_RECOGNIZED")
            if exc.code == "PARSER_FORMAT_NOT_RECOGNIZED" and legacy_parser is None:
                for document in kept:
                    if document.document_id in selections:
                        continue
                    try:
                        profile = profile_bytes(verified_sources[document.document_id],
                                                PurePosixPath(document.blob_path).suffix)
                    except RegistryError:
                        raise ProcessingError(f"registry:{exc.code}") from exc
                    profiles.setdefault(profile.profile_hash, (profile, []))[1].append(document)
                for profile, documents in profiles.values():
                    evidence = tuple({"document_id": d.document_id, "blob_path": d.blob_path,
                                      "size_bytes": d.size_bytes, "sha256": d.sha256.lower()}
                                     for d in documents)
                    candidate = persist_candidate(self.storage, request.parser_key,
                                                  request.customer_key, profile, evidence,
                                                self.candidate_clock, stable_order=self._budget_enabled(request),
                                                package_choice=self._choice_ref())
                    pending.append(CandidateRequiredError(
                        candidate.request_id, request.parser_key, profile.profile_hash,
                        tuple(d.document_id for d in documents)))
                if (len(pending) == 1 and not selections
                        and len(pending[0].document_ids) == 1
                        and len(request.documents) == 1 and choice is None):
                    if not self._budget_enabled(request):
                        raise pending[0] from exc
                self._raise_grouped(request, selections, pending, exc)
            else:
                if exc.code != "REGISTRY_ABSENT":
                    raise ProcessingError(f"registry:{exc.code}") from exc
        if profiles:
            for profile, documents in profiles.values():
                evidence = tuple({"document_id": d.document_id, "blob_path": d.blob_path,
                                  "size_bytes": d.size_bytes, "sha256": d.sha256.lower()}
                                 for d in documents)
                candidate = persist_candidate(self.storage, request.parser_key,
                                              request.customer_key, profile, evidence,
                                            self.candidate_clock, stable_order=self._budget_enabled(request),
                                            package_choice=self._choice_ref())
                pending.append(CandidateRequiredError(
                    candidate.request_id, request.parser_key, profile.profile_hash,
                    tuple(d.document_id for d in documents)))
            if (len(pending) == 1 and not selections
                    and len(pending[0].document_ids) == 1
                    and len(request.documents) == 1 and choice is None):
                if not self._budget_enabled(request):
                    raise pending[0]
            self._raise_grouped(request, selections, pending)
        if pending:
            raise AggregateCandidateRequiredError(tuple(pending))
        if choice is not None and len(kept) != len(request.documents):
            # Every kept BOM already has an approved parser: the direct manifest would omit the set-aside
            # files, which the processing contract does not allow. Not built yet; stop loudly.
            raise ProcessingError("package choice approved-only path unsupported")
        claim = {"version": CONTRACT_VERSION, "job_id": request.job_id,
                 "request_fingerprint": fingerprint, "status": "started"}
        prior = self.storage.download_output(claim_path, self.max_output_bytes)
        if prior is not None:
            try: value = json.loads(prior)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc: raise BlobConflictError("existing claim is invalid") from exc
            if not isinstance(value, dict) or value.get("request_fingerprint") != fingerprint:
                raise BlobConflictError("job id already used by a different request")
        else:
            self.storage.upload_output_immutable(claim_path, _json_bytes(claim), "application/json")
        results = []
        for document in request.documents:
            source = verified_sources.get(document.document_id)
            if source is None:
                source = self.storage.download_source(document.blob_path, self.max_source_bytes)
            if len(source) != document.size_bytes or _sha256(source).lower() != document.sha256.lower():
                raise ProcessingError("source integrity check failed")
            worker_job_id = f"{request.job_id}-{document.document_id}"
            selected = selections.get(document.document_id)
            worker_prefix = f"{prefix}/worker/{worker_job_id}"
            # Sub-assembly plan R3: a variant whose provenance shows it was validated with a row map
            # is asked for one on reuse, and its output is row-checked like a candidate's.
            row_checked = selected is not None and selected.row_map
            item = WorkItem(worker_job_id, "boms", "parsers", "boms", document.blob_path,
                            selected.variant.code_path if selected else f"{request.parser_key}/parse.py", document.sha256.lower(), document.size_bytes,
                            _sha256(selected.code) if selected else _sha256(legacy_parser), len(selected.code) if selected else len(legacy_parser), worker_prefix,
                            f"input{PurePosixPath(document.blob_path).suffix.lower()}",
                            "output.xlsx", "result.json", row_map_name=ROW_MAP_NAME if row_checked else None)
            try:
                started = self.orchestrator.start(item)
                raw = self._poll(item, started.execution_name)
            except ProcessingError:
                raise
            except Exception as exc:
                raise ProcessingError("job failed") from exc
            output, mapped = self._worker_result(raw, item)
            worker_bytes = self.storage.download_output(item.output_blob, self.max_output_bytes)
            if worker_bytes is None:
                raise ProcessingError("worker output missing")
            _validate_xlsx_bytes(worker_bytes, self.max_rows)
            accounting = _reuse_row_accounting(source, PurePosixPath(document.blob_path).suffix.lower(),
                                               worker_bytes, item, mapped) if selected is not None else None
            final_path = f"{prefix}/{document.document_id}-parsed.xlsx"
            self.storage.upload_output_immutable(final_path, worker_bytes, OUTPUT_CONTENT_TYPE)
            result = {"source": {"document_id": document.document_id, "blob_path": document.blob_path,
                         "size_bytes": document.size_bytes, "sha256": document.sha256.lower()},
                         "output": {"blob_path": final_path, "size_bytes": len(worker_bytes),
                                    "sha256": _sha256(worker_bytes), "content_type": OUTPUT_CONTENT_TYPE}}
            if selected is not None:
                result["parser"] = self._result_parser_provenance(request.parser_key, selected)
                # Beside, never inside, "parser": package_resolution checks those keys exactly.
                result["row_accounting"] = accounting
            results.append(result)
        manifest = {"version": CONTRACT_VERSION, "status": "completed", "job_id": request.job_id,
                    "request_fingerprint": fingerprint, "customer_key": request.customer_key,
                    "rfq_uuid": request.rfq_uuid.lower(), "package_version": request.package_version,
                    "parser": {"key": request.parser_key, "blob_path": selections[request.documents[0].document_id].variant.code_path if legacy_parser is None else f"{request.parser_key}/parse.py", "sha256": _sha256(selections[request.documents[0].document_id].code) if legacy_parser is None else _sha256(legacy_parser)},
                    "results": results}
        selection = selections.get(request.documents[0].document_id)
        if selection:
            manifest["parser"].update({"variant_id": selection.variant.variant_id, "version": selection.variant.version,
                "signature_hash": selection.variant.signature_hash, "rule_hash": selection.variant.rule_hash,
                "approval_id": selection.variant.approval_id, "selection_mode": "registry",
                "registry_generation": selection.generation})
        else:
            manifest["parser"]["selection_mode"] = "legacy"
        self.storage.upload_output_immutable(manifest_path, _json_bytes(manifest), "application/json")
        return manifest

    @staticmethod
    def _result_parser_provenance(parser_key: str, selection: Any) -> dict[str, Any]:
        variant = selection.variant
        return {"key": parser_key, "variant_id": variant.variant_id,
                "version": variant.version, "approval_id": variant.approval_id,
                "code_path": variant.code_path, "code_sha256": variant.code_sha256,
                "signature_hash": variant.signature_hash, "rule_hash": variant.rule_hash,
                "registry_generation": selection.generation}

    @staticmethod
    def _approved_assignments(selections: dict[str, Any]) -> list[dict[str, Any]]:
        grouped: dict[tuple[str, str, str, str], list[str]] = {}
        for document_id, selection in selections.items():
            variant = selection.variant
            key = (variant.variant_id, variant.version, variant.approval_id, variant.code_sha256)
            grouped.setdefault(key, []).append(document_id)
        return [{"document_ids": sorted(document_ids), "variant_id": key[0],
                 "version": key[1], "approval_id": key[2], "code_sha256": key[3]}
                for key, document_ids in sorted(grouped.items())]

    def _raise_grouped(self, request: ProcessingRequest, selections: dict[str, Any],
                       pending: list[CandidateRequiredError], cause: Exception | None = None) -> None:
        from .package_resolution import ParentPackageService
        plan = ParentPackageService(
            self.storage, max_source_bytes=self.max_source_bytes,
            max_output_bytes=self.max_output_bytes, max_rows=self.max_rows).plan(
                request,
                approved_assignments=self._approved_assignments(selections),
                pending_assignments=[{"request_id": item.request_id,
                                      "document_ids": list(item.document_ids)}
                                     for item in pending],
                package_choice=getattr(self, "_choice", None))
        if self._budget_enabled(request):
            from .author_budget import Budget, enroll
            for item in pending:
                if Budget(self.storage, item.request_id).inspect()["status"] == "legacy":
                    continue  # Existing opt-out requests are never backfilled.
                enroll(self.storage, plan, item.request_id)
        error = AggregateCandidateRequiredError(tuple(pending), plan["plan_id"])
        if cause is not None:
            raise error from cause
        raise error

    def _choice_ref(self) -> dict[str, str] | None:
        choice = getattr(self, "_choice", None)
        if choice is None:
            return None
        from .bom_package_choice import choice_reference
        return choice_reference(choice)

    def _require_candidate(self, request: ProcessingRequest,
                           verified_sources: dict[str, bytes], cause: Exception,
                           documents: tuple[Any, ...] | None = None) -> None:
        documents = request.documents if documents is None else documents
        groups: dict[str, tuple[Any, list[Any]]] = {}
        for document in documents:
            profile = profile_bytes(verified_sources[document.document_id],
                                    PurePosixPath(document.blob_path).suffix)
            groups.setdefault(profile.profile_hash, (profile, []))[1].append(document)
        errors = []
        for profile, documents in groups.values():
            evidence = tuple({"document_id": d.document_id, "blob_path": d.blob_path,
                              "size_bytes": d.size_bytes, "sha256": d.sha256.lower()}
                             for d in documents)
            candidate = persist_candidate(self.storage, request.parser_key,
                                          request.customer_key, profile, evidence,
                                          self.candidate_clock, stable_order=self._budget_enabled(request),
                                          package_choice=self._choice_ref())
            errors.append(CandidateRequiredError(candidate.request_id, request.parser_key,
                                                  profile.profile_hash,
                                                  tuple(d.document_id for d in documents)))
        if (len(errors) == 1 and len(request.documents) == 1 and not self._budget_enabled(request)
                and getattr(self, "_choice", None) is None):
            raise errors[0] from cause
        self._raise_grouped(request, {}, errors, cause)

    def _poll(self, item: WorkItem, execution: str) -> bytes:
        deadline = self.clock() + self.timeout_seconds
        while self.clock() <= deadline:
            value = self.storage.download_output(item.result_blob, self.max_output_bytes)
            if value is not None:
                return value
            self.sleep(self.poll_interval_seconds)
        raise ProcessingError("job timed out")

    def _worker_result(self, raw: bytes, item: WorkItem) -> tuple[bytes, bytes | None]:
        """The verified output bytes and, when one was asked for, the verified row map bytes."""
        try: value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc: raise ProcessingError("worker result schema invalid") from exc
        contract = "remote-worker.v3" if item.row_map_name else "remote-worker.v1"
        if (not isinstance(value, dict) or value.get("contract_version") != contract
                or value.get("job") != item.job_id or value.get("status") != "completed"):
            raise ProcessingError("worker result schema invalid")
        for section, expected_hash, expected_size in (("input", item.input_sha256, item.input_size), ("parser", item.parser_sha256, item.parser_size)):
            evidence = value.get(section)
            if (not isinstance(evidence, dict) or evidence.get("verified") is not True
                    or evidence.get("sha256", "").lower() != expected_hash.lower() or evidence.get("size") != expected_size):
                raise ProcessingError("worker evidence invalid")
        output = value.get("output")
        if (not isinstance(output, dict) or output.get("content_type") != OUTPUT_CONTENT_TYPE
                or type(output.get("size")) is not int or not 0 < output["size"] <= self.max_output_bytes
                or not isinstance(output.get("sha256"), str)):
            raise ProcessingError("worker output schema invalid")
        worker_bytes = self.storage.download_output(item.output_blob, self.max_output_bytes)
        if worker_bytes is None or len(worker_bytes) != output["size"] or _sha256(worker_bytes).lower() != output["sha256"].lower():
            raise ProcessingError("worker output hash mismatch")
        if not item.row_map_name:
            return worker_bytes, None
        # The row map's integrity is a hard check (as in candidate validation); only the row
        # check's verdict is a warning on reuse (Nick 2026-10-12).
        row_map = value.get("row_map")
        if (not isinstance(row_map, dict) or row_map.get("content_type") != "application/json"
                or type(row_map.get("size")) is not int or not 0 < row_map["size"] <= MAX_ROW_MAP_BYTES
                or not isinstance(row_map.get("sha256"), str)):
            raise ProcessingError("worker row map schema invalid")
        mapped = self.storage.download_output(item.row_map_blob, MAX_ROW_MAP_BYTES)
        if mapped is None or len(mapped) != row_map["size"] or _sha256(mapped).lower() != row_map["sha256"].lower():
            raise ProcessingError("worker row map hash mismatch")
        return worker_bytes, mapped

    def _validate_manifest(self, raw: bytes, request: ProcessingRequest, fingerprint: str) -> dict[str, Any]:
        try: value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc: raise BlobConflictError("existing manifest is invalid") from exc
        if not isinstance(value, dict) or value.get("request_fingerprint") != fingerprint or value.get("status") != "completed":
            raise BlobConflictError("job id already used by a different request")
        return value


def _reuse_row_accounting(source: bytes, suffix: str, output: bytes, item: WorkItem,
                          mapped: bytes | None) -> dict[str, Any]:
    """Row check of a reused approved parser's output (sub-assembly plan R3, Nick 2026-10-12).

    A failed check is a warning carried with the result, never a stop. Counts and codes only; the
    row map stays in the output store (its path and hash are recorded) for the structure step.
    A variant approved before provenance existed is reused unchecked, and says so.
    """
    if mapped is None:
        return {"checked": False, "reason": "PARSER_APPROVED_WITHOUT_ROW_MAP"}
    try:
        verified = verify_row_map(source, suffix, output, mapped)
    except RowMapError as exc:
        verified = {"version": ROW_ACCOUNTING_RESULT_VERSION, "valid": False, "error": str(exc)}
    except Exception:
        verified = {"version": ROW_ACCOUNTING_RESULT_VERSION, "valid": False, "error": "ROW_MAP_UNVERIFIABLE"}
    summary = {key: verified[key] for key in (
        "version", "valid", "error", "source_rows", "line_rows", "skipped_rows", "output_lines",
        "failed_row_count", "failure_codes", "maker_code_prefix_kept", "structure") if key in verified}
    summary["unmapped_column_count"] = len(verified.get("unmapped_columns", []))
    summary["pattern_finding_count"] = len(verified.get("pattern_findings", []))
    return {"checked": True, **summary,
            "row_map": {"blob_path": item.row_map_blob, "sha256": _sha256(mapped), "size": len(mapped)}}


def _validate_xlsx_bytes(data: bytes, max_rows: int) -> None:
    import tempfile
    from pathlib import Path
    # Close the file before pandas reopens it; Windows denies reopening an
    # active NamedTemporaryFile handle.
    with tempfile.TemporaryDirectory(prefix="agentquote-validate-") as directory:
        path = Path(directory) / "output.xlsx"
        path.write_bytes(data)
        _validate_xlsx(path, max_rows)