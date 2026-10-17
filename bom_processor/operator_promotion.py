"""Operator promotion: register a validated candidate's parser in the customer's registry.

Purpose: a parser a person has already validated for one quote (operator candidate or model
candidate, awaiting_approval with every fixture passed) becomes an approved registry variant, so the
next package from the same customer reuses it (registry_reuse) instead of authoring. Registration
goes through AdminDecisionService (the same evidence, binding and write-once checks as
POST /api/v1/parser-candidates/decision) with a named approver; nothing here is a second path.

Refusals (PromotionRefused, the message is a code), all checked before any write; every decide()
check (bindings, fixture selections, mapping) also runs first on a recording view (dry run):
  CANDIDATE_NOT_VALIDATED   the selected candidate is not awaiting_approval with a validation
  SIGNATURE_MISMATCH        the variant's own signature does not match every source of the request
  SIGNATURE_AMBIGUOUS       an existing variant matches a source of this request, or has the same rule
  VARIANT_EXISTS            the variant id and version are already registered
Run: python -m pytest tests/test_operator_promotion.py
"""
from __future__ import annotations

import json
from pathlib import PurePosixPath
from typing import Any

MAX_SOURCE_BYTES = 50_000_000


class PromotionRefused(ValueError):
    """Stable refusal before any write."""


def _json(storage: Any, path: str, candidate: bool = False) -> dict[str, Any]:
    read = storage.download_candidate if candidate else storage.download_registry
    return json.loads(read(path, 1_000_000).decode("utf-8"))


def _existing_variants(storage: Any, parser_key: str) -> list[tuple[dict[str, Any], Any]]:
    from .core import BlobNotFoundError
    from .master_registry import VariantSignature
    try:
        master = _json(storage, f"registry/{parser_key}/master.json")
    except (BlobNotFoundError, FileNotFoundError):
        return []
    result = []
    for item in master.get("variants", []):
        signature = VariantSignature.from_dict(_json(
            storage, f"approved/{parser_key}/{item['variant_id']}/{item['version']}/signature.json"))
        result.append((item, signature))
    return result


def plan(storage: Any, request_id: str, *, variant_id: str, version: str) -> dict[str, Any]:
    """Every pre-write check; returns the decision inputs and the signature summary."""
    from .admin_promotion import AdminDecisionService
    from .candidate_review import CandidateReviewService
    from .candidate_status import CandidateStatusService
    from .candidates import unique_fixtures
    from .master_registry import _hash, profile_bytes
    status = CandidateStatusService(storage).get(request_id)
    candidate_id, validation_id = status.get("candidate_id"), status.get("validation_id")
    if status.get("status") != "awaiting_approval" or not candidate_id or not validation_id:
        raise PromotionRefused("CANDIDATE_NOT_VALIDATED")
    review = CandidateReviewService(storage).get_v3(request_id, candidate_id, validation_id)
    if not review.get("validation_complete"):
        raise PromotionRefused("CANDIDATE_NOT_VALIDATED")
    request = _json(storage, f"candidates/requests/{request_id}.json")
    evidence = _json(storage, f"candidates/{request_id}/{candidate_id}/validation/{validation_id}/evidence.json", True)
    manifest_sha256 = review.get("evidence", {}).get("manifest_sha256")
    if not isinstance(manifest_sha256, str):
        manifest_sha256 = _hash(evidence.get("fixtures", []))
    signature = AdminDecisionService._signature(request, evidence)
    parser_key = request["parser_key"]
    existing = _existing_variants(storage, parser_key)
    if any(item.get("variant_id") == variant_id and item.get("version") == version for item, _ in existing):
        raise PromotionRefused("VARIANT_EXISTS")
    if any(other.rule_hash == signature.rule_hash for _, other in existing):
        raise PromotionRefused("SIGNATURE_AMBIGUOUS")
    sources = []
    for fixture in unique_fixtures(request["source_evidence"]):
        data = storage.download_source(fixture["blob_path"], MAX_SOURCE_BYTES)
        profile = profile_bytes(data, PurePosixPath(fixture["blob_path"]).suffix)
        if not signature.matches(profile):
            raise PromotionRefused("SIGNATURE_MISMATCH")
        clashes = sorted(f"{item['variant_id']}/{item['version']}" for item, other in existing
                         if other.matches(profile))
        if clashes:
            raise PromotionRefused("SIGNATURE_AMBIGUOUS")
        sources.append({"name": PurePosixPath(fixture["blob_path"]).name, "sha256": fixture["sha256"]})
    return {"request_id": request_id, "candidate_id": candidate_id, "validation_id": validation_id,
            "code_sha256": status.get("code_hash"), "parser_key": parser_key,
            "variant_id": variant_id, "version": version,
            "review_manifest_sha256": manifest_sha256,
            "signature": {"extensions": sorted(signature.extensions),
                          "sheet_names": sorted(signature.required_sheet_names),
                          "heading_rule": signature.required_heading is not None,
                          "heading_row": {name: list(row) for name, row in (signature.required_heading or {}).items()},
                          "required_headers": {name: sorted(row) for name, row in signature.required_headers.items()},
                          "rule_hash": signature.rule_hash, "signature_hash": signature.signature_hash},
            "conflict_groups": [{"group_id": group.get("group_id"),
                                 "fixtures": sorted(item.get("fixture_sha256") for item in group.get("fixtures", [])),
                                 "selected_fixture_sha256": group.get("selected_fixture_sha256")}
                                for group in review.get("groups", []) if group.get("has_conflicts") is True],
            "existing_variants": [f"{item['variant_id']}/{item['version']}" for item, _ in existing],
            "sources_matched": sources}


def decide(storage: Any, checked: dict[str, Any], *, approver: str, reason: str, ticket: str = "",
           fixture_selections: dict[str, str] | None = None, hold_writer: bool = True,
           clock: Any = None) -> dict[str, Any]:
    """Approve through AdminDecisionService. hold_writer=False only for a dry run on a recording view.

    fixture_selections names, for each review group with conflicting fixtures, the fixture the
    operator chose (group_id -> fixture sha256), exactly as the decision API takes it.
    """
    from .admin_promotion import AdminDecisionService
    service = AdminDecisionService(storage, clock=clock)
    arguments = dict(request_id=checked["request_id"], candidate_id=checked["candidate_id"],
                     validation_id=checked["validation_id"], decision="approve",
                     approver={"object_id": approver}, reason=reason, ticket=ticket,
                     variant_id=checked["variant_id"], version=checked["version"],
                     review_manifest_sha256=checked["review_manifest_sha256"],
                     fixture_selections=dict(fixture_selections or {}))
    return (service.decide if hold_writer else service._decide_locked)(**arguments)
