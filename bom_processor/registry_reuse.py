"""Reuse an approved parser for a package child before any model authoring (QQ demo, 2026-10-14).

A package child candidate request whose every source matches the same single approved variant in
the customer's registry gets that parser as its candidate, through the normal authoring boundary
(CandidateAuthoringService with a fixed author: same code policy, same immutable parse.py, manifest
and generated event). One selection record (operator_candidate) makes the status projection choose
it, and one write-once provenance record names the registry variant it came from. Validation then
runs as for any candidate and a person still approves the package. Nothing is approved here.

No match, an absent registry, an ambiguous match, or sources matching different variants all return
None, so the caller keeps today's model-authoring path. Any other registry error (corrupt registry,
unreadable source) is raised.

Writes (registry/candidate containers, write-once):
  candidates/<request>/<candidate>/parse.py, candidate-manifest.json,
  candidates/events/<request>/<candidate>.json,
  candidates/operator-selections/<request>/<candidate>.json,
  candidates/registry-reuse/<request>/<candidate>.json
Run: python -m pytest tests/test_registry_reuse.py
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any, Callable

from .core import BlobConflictError

REUSE_VERSION = "registry-reuse.v1"
REUSE_ROOT = "candidates/registry-reuse"
REUSE_ACTOR = "registry-reuse"
MAX_SOURCE_BYTES = 50_000_000
# A selection that leaves the request on today's authoring path.
FALLBACK_CODES = frozenset({"REGISTRY_ABSENT", "PARSER_FORMAT_NOT_RECOGNIZED", "PARSER_FORMAT_AMBIGUOUS"})


class RegistryReuseError(ValueError):
    """Stable, fail-closed refusal; the message is a code."""


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def match_request(storage: Any, request: dict[str, Any], *, reader: Any = None):
    """The single registry selection every source of the request matches, or None.

    Returns (selection, outcome) where outcome names why reuse does or does not apply.
    """
    from .blob_registry import BlobRegistryReader
    from .candidates import unique_fixtures
    from .master_registry import RegistryError
    reader = reader or BlobRegistryReader(storage.download_registry)
    chosen = None
    for fixture in unique_fixtures(request["source_evidence"]):
        data = storage.download_source(fixture["blob_path"], MAX_SOURCE_BYTES)
        if len(data) != fixture["size_bytes"] or hashlib.sha256(data).hexdigest() != fixture["sha256"].lower():
            raise RegistryReuseError("SOURCE_INTEGRITY")
        try:
            selection = reader.select(request["parser_key"], request["customer_key"], data,
                                      PurePosixPath(fixture["blob_path"]).suffix)
        except RegistryError as exc:
            if exc.code in FALLBACK_CODES:
                return None, exc.code.lower()
            raise
        identity = (selection.variant.variant_id, selection.variant.version,
                    selection.variant.approval_id, selection.variant.code_sha256)
        if chosen is not None and (chosen.variant.variant_id, chosen.variant.version,
                                   chosen.variant.approval_id, chosen.variant.code_sha256) != identity:
            return None, "sources_match_different_variants"
        chosen = selection
    if chosen is None:
        return None, "no_sources"
    return chosen, "matched"


def reuse_path(request_id: str, candidate_id: str) -> str:
    from .operator_candidate import selection_path
    selection_path(request_id, candidate_id)  # same id validation
    return f"{REUSE_ROOT}/{request_id}/{candidate_id}.json"


def reuse(storage: Any, request_id: str, *, clock: Callable[[], datetime] | None = None,
          reader: Any = None) -> dict[str, Any] | None:
    """Author the approved parser as this request's candidate when exactly one variant matches.

    The caller holds the request's writer and has checked that no candidate exists yet. Returns the
    provenance record, or None when reuse does not apply (today's path continues).
    """
    from .operator_candidate import FixedCodeAuthor, select, selection_record
    from .candidate_authoring import CandidateAuthoringService
    request = json.loads(storage.download_registry(f"candidates/requests/{request_id}.json", 1_000_000))
    if request.get("request_id") != request_id or request.get("status") != "requested":
        return None
    selection, outcome = match_request(storage, request, reader=reader)
    if selection is None:
        return None
    try:
        code = selection.code.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RegistryReuseError("REGISTRY_CODE_INVALID") from exc
    authored = CandidateAuthoringService(request_id, storage, FixedCodeAuthor(code)).author()
    manifest = json.loads(storage.download_candidate(authored.manifest_path, 1_000_000))
    if manifest.get("code_sha256") != selection.variant.code_sha256:
        # The authoring boundary must store the approved bytes unchanged.
        raise RegistryReuseError("REGISTRY_CODE_CHANGED")
    variant = selection.variant
    reason = (f"approved parser {variant.variant_id}/{variant.version} (approval {variant.approval_id[:16]}) "
              f"matched every source")
    select(storage, selection_record(request_id=request_id, candidate_id=authored.candidate_id,
                                     code_sha256=variant.code_sha256, actor=REUSE_ACTOR,
                                     reason=reason, clock=clock))
    record = {"version": REUSE_VERSION, "request_id": request_id, "candidate_id": authored.candidate_id,
              "parser_key": variant.parser_key, "customer_key": request["customer_key"],
              "variant_id": variant.variant_id, "variant_version": variant.version,
              "approval_id": variant.approval_id, "code_path": variant.code_path,
              "code_sha256": variant.code_sha256, "signature_hash": variant.signature_hash,
              "rule_hash": variant.rule_hash, "registry_generation": selection.generation,
              "sources": sorted(item["sha256"].lower() for item in request["source_evidence"])}
    path = reuse_path(request_id, authored.candidate_id)
    try:
        storage.upload_registry_immutable(path, _json_bytes(record), "application/json")
    except BlobConflictError:
        prior = json.loads(storage.download_registry(path, 100_000))
        if prior != record:
            raise RegistryReuseError("REGISTRY_REUSE_CONFLICT") from None
    return record
