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


class RemoteBomProcessor:
    def __init__(self, storage: Storage, orchestrator: JobOrchestrator, *, timeout_seconds=60,
                 poll_interval_seconds=1, max_source_bytes=50_000_000,
                 max_total_source_bytes=100_000_000, max_parser_bytes=1_000_000,
                 max_output_bytes=50_000_000, max_rows=100_000,
                 registry_reader: BlobRegistryReader | None = None,
                 candidate_clock=None,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
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
        reader = getattr(storage, "download_registry", None)
        self.registry_reader = registry_reader or (BlobRegistryReader(reader, max_bytes=max_parser_bytes) if reader else None)

    def process(self, request: ProcessingRequest) -> dict[str, Any]:
        request.validate()
        if any(d.size_bytes > self.max_source_bytes for d in request.documents):
            raise ProcessingError("source exceeds limit")
        if sum(d.size_bytes for d in request.documents) > self.max_total_source_bytes:
            raise ProcessingError("request exceeds aggregate source limit")
        prefix = f"{request.customer_key}/rfq-{request.rfq_uuid}/package-{request.package_version}/processed/{request.job_id}"
        manifest_path, claim_path = f"{prefix}/manifest.json", f"{prefix}/claim.json"
        fingerprint = request.fingerprint()
        if request.force_candidate:
            verified_sources = {}
            for document in request.documents:
                source = self.storage.download_source(document.blob_path, self.max_source_bytes)
                if len(source) != document.size_bytes or _sha256(source).lower() != document.sha256.lower():
                    raise ProcessingError("source integrity check failed")
                verified_sources[document.document_id] = source
            self._require_candidate(request, verified_sources, ProcessingError("replacement requested"))
        existing = self.storage.download_output(manifest_path, self.max_output_bytes)
        if existing is not None:
            return self._validate_manifest(existing, request, fingerprint)
        verified_sources: dict[str, bytes] = {}
        for document in request.documents:
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
            for document in request.documents:
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
                for document in request.documents:
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
                                                  self.candidate_clock)
                    pending.append(CandidateRequiredError(
                        candidate.request_id, request.parser_key, profile.profile_hash,
                        tuple(d.document_id for d in documents)))
                if len(pending) == 1:
                    raise pending[0] from exc
                raise AggregateCandidateRequiredError(tuple(pending)) from exc
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
                                              self.candidate_clock)
                pending.append(CandidateRequiredError(
                    candidate.request_id, request.parser_key, profile.profile_hash,
                    tuple(d.document_id for d in documents)))
            if len(pending) == 1:
                raise pending[0]
            raise AggregateCandidateRequiredError(tuple(pending))
        if pending:
            raise AggregateCandidateRequiredError(tuple(pending))
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
            item = WorkItem(worker_job_id, "boms", "parsers", "boms", document.blob_path,
                            selected.variant.code_path if selected else f"{request.parser_key}/parse.py", document.sha256.lower(), document.size_bytes,
                            _sha256(selected.code) if selected else _sha256(legacy_parser), len(selected.code) if selected else len(legacy_parser), worker_prefix,
                            f"input{PurePosixPath(document.blob_path).suffix.lower()}",
                            "output.xlsx", "result.json")
            try:
                started = self.orchestrator.start(item)
                raw = self._poll(item, started.execution_name)
            except ProcessingError:
                raise
            except Exception as exc:
                raise ProcessingError("job failed") from exc
            output = self._worker_result(raw, item)
            worker_bytes = self.storage.download_output(item.output_blob, self.max_output_bytes)
            if worker_bytes is None:
                raise ProcessingError("worker output missing")
            _validate_xlsx_bytes(worker_bytes, self.max_rows)
            final_path = f"{prefix}/{document.document_id}-parsed.xlsx"
            self.storage.upload_output_immutable(final_path, worker_bytes, OUTPUT_CONTENT_TYPE)
            results.append({"source": {"document_id": document.document_id, "blob_path": document.blob_path,
                         "size_bytes": document.size_bytes, "sha256": document.sha256.lower()},
                         "output": {"blob_path": final_path, "size_bytes": len(worker_bytes),
                                    "sha256": _sha256(worker_bytes), "content_type": OUTPUT_CONTENT_TYPE}})
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

    def _require_candidate(self, request: ProcessingRequest,
                           verified_sources: dict[str, bytes], cause: Exception) -> None:
        groups: dict[str, tuple[Any, list[Any]]] = {}
        for document in request.documents:
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
                                          self.candidate_clock)
            errors.append(CandidateRequiredError(candidate.request_id, request.parser_key,
                                                  profile.profile_hash,
                                                  tuple(d.document_id for d in documents)))
        if len(errors) == 1:
            raise errors[0] from cause
        raise AggregateCandidateRequiredError(tuple(errors)) from cause

    def _poll(self, item: WorkItem, execution: str) -> bytes:
        deadline = self.clock() + self.timeout_seconds
        while self.clock() <= deadline:
            value = self.storage.download_output(item.result_blob, self.max_output_bytes)
            if value is not None:
                return value
            self.sleep(self.poll_interval_seconds)
        raise ProcessingError("job timed out")

    def _worker_result(self, raw: bytes, item: WorkItem) -> bytes:
        try: value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc: raise ProcessingError("worker result schema invalid") from exc
        if (not isinstance(value, dict) or value.get("contract_version") != "remote-worker.v1"
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
        return worker_bytes

    def _validate_manifest(self, raw: bytes, request: ProcessingRequest, fingerprint: str) -> dict[str, Any]:
        try: value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc: raise BlobConflictError("existing manifest is invalid") from exc
        if not isinstance(value, dict) or value.get("request_fingerprint") != fingerprint or value.get("status") != "completed":
            raise BlobConflictError("job id already used by a different request")
        return value


def _validate_xlsx_bytes(data: bytes, max_rows: int) -> None:
    import tempfile
    from pathlib import Path
    # Close the file before pandas reopens it; Windows denies reopening an
    # active NamedTemporaryFile handle.
    with tempfile.TemporaryDirectory(prefix="agentquote-validate-") as directory:
        path = Path(directory) / "output.xlsx"
        path.write_bytes(data)
        _validate_xlsx(path, max_rows)