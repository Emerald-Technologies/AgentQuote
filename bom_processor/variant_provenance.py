"""Provenance of an approved parser variant (sub-assembly plan R3, Nick 2026-10-12).

Written once at promotion as approved/{key}/{variant}/{version}/provenance.json, beside the
immutable approval and signature (which are read strictly and are not changed). It records where
the parser came from and whether it was validated with a row map, so reuse on a new document can
ask for one: passing --row-map to a parser that does not support it fails the worker job.
A variant approved before this record existed has none and is reused as before, with a note that
its output was not row-checked.

Inputs: the candidate manifest and validation evidence (promotion) or the stored record and the
approval it must match (selection). Pure; no I/O.
"""
from __future__ import annotations

from typing import Any, Mapping

VERSION = "variant-provenance.v1"
NAME = "provenance.json"
_KEYS = {"version", "approval_id", "request_id", "candidate_id", "validation_id", "authoring_version",
         "candidate_manifest_sha256", "row_map"}


def row_accounted(evidence: Mapping[str, Any]) -> bool:
    """True when every validated file of the evidence (single or grouped) passed the row check."""
    fixtures = evidence.get("fixtures")
    grouped = isinstance(fixtures, list) and any(isinstance(item, dict) and "fixture_sha256" in item
                                                 for item in fixtures)
    records = fixtures if grouped else [evidence]
    if not records:
        return False
    for record in records:
        semantic = record.get("semantic") if isinstance(record, dict) else None
        accounting = semantic.get("row_accounting") if isinstance(semantic, dict) else None
        if not (isinstance(accounting, dict) and accounting.get("valid") is True):
            return False
    return True


def build(manifest: Mapping[str, Any], evidence: Mapping[str, Any], *, approval_id: str, request_id: str,
          candidate_id: str, validation_id: str, candidate_manifest_sha256: str) -> dict[str, Any]:
    from .candidate_authoring import ROW_MAP_AUTHORING_VERSIONS
    authoring = manifest.get("version")
    return {"version": VERSION, "approval_id": approval_id, "request_id": request_id,
            "candidate_id": candidate_id, "validation_id": validation_id,
            "authoring_version": authoring if isinstance(authoring, str) else None,
            "candidate_manifest_sha256": candidate_manifest_sha256,
            "row_map": authoring in ROW_MAP_AUTHORING_VERSIONS and row_accounted(evidence)}


def row_map_supported(record: Mapping[str, Any], approval: Any) -> bool:
    """The record's row-map flag after checking it belongs to this approval; ValueError otherwise."""
    if (set(record) != _KEYS or record["version"] != VERSION or type(record["row_map"]) is not bool
            or record["approval_id"] != approval.approval_id or record["candidate_id"] != approval.candidate_id
            or record["candidate_manifest_sha256"] != approval.candidate_hash):
        raise ValueError("provenance does not match the approval")
    return record["row_map"]
