"""Bounded successor-attempt contract for evidence regeneration.

Input: server-built successor binding with code-generated qualification feedback.
Output: detached validated value. Feedback carries IDs/codes only, never source text.
Run: python -m pytest tests/test_package_evidence_successor.py.
"""
from __future__ import annotations

import re
from typing import Any

from .package_evidence_contracts import detached, hash_value, integer, require, strict, text, uuid_value

MAX_ATTEMPT = 5
MAX_FINDINGS = 200
CHECK_KINDS = {"inventory", "blocking_findings", "applicability", "identity_revision", "relationship", "sharing", "exclusion"}
CORRECTION_INSTRUCTION = (
    "A prior proposal for this exact package failed independent qualification. correctionFeedback is "
    "code-generated and lists each failed check by kind, subject, reasonCode and the evidence/assemblies involved; "
    "it is not evidence. Re-read the complete sources. Link evidence only to assemblies whose part number and "
    "revision the cited source text explicitly identifies. Use shared scope only when cited source text explicitly "
    "names every linked assembly. Citations must quote the identifying source lines, not warnings or headers. "
    "When sources do not prove a link, use assembly_specific links, unresolved dispositions or unknown "
    "determinations instead of repeating the failed claim. Never fabricate support to pass a check."
)


def _finding(value: Any) -> dict:
    strict(value, {"kind", "subject", "reasonCode", "evidenceIds", "association"}, "INVALID_SUCCESSOR_FEEDBACK")
    require(value["kind"] in CHECK_KINDS, "INVALID_SUCCESSOR_FEEDBACK")
    text(value["subject"])
    require(isinstance(value["reasonCode"], str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", value["reasonCode"]) is not None,
            "INVALID_SUCCESSOR_FEEDBACK")
    ids = value["evidenceIds"]
    require(type(ids) is list and len(ids) <= 50 and len(set(ids)) == len(ids), "INVALID_SUCCESSOR_FEEDBACK")
    for item in ids:
        hash_value(item)
    association = value["association"]
    if association is not None:
        strict(association, {"evidenceId", "assemblyUuids", "scope"}, "INVALID_SUCCESSOR_FEEDBACK")
        hash_value(association["evidenceId"])
        assemblies = association["assemblyUuids"]
        require(type(assemblies) is list and 1 <= len(assemblies) <= 50 and len(set(assemblies)) == len(assemblies),
                "INVALID_SUCCESSOR_FEEDBACK")
        for item in assemblies:
            uuid_value(item)
        require(association["scope"] in {"shared", "assembly_specific"}, "INVALID_SUCCESSOR_FEEDBACK")
    return value


def validate_successor(value: Any) -> dict:
    strict(value, {"predecessorProposalSha256", "attempt", "feedback"}, "INVALID_SUCCESSOR")
    hash_value(value["predecessorProposalSha256"])
    integer(value["attempt"], 2, MAX_ATTEMPT)
    feedback = strict(value["feedback"], {"qualificationSha256", "findings"}, "INVALID_SUCCESSOR_FEEDBACK")
    hash_value(feedback["qualificationSha256"])
    findings = feedback["findings"]
    require(type(findings) is list and 1 <= len(findings) <= MAX_FINDINGS, "INVALID_SUCCESSOR_FEEDBACK")
    for item in findings:
        _finding(item)
    return detached(value)
