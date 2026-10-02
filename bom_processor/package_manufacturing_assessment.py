"""Source-grounded requirement/role artifact; source quotes are not approval.

Inputs: verified graph/classification/complete text and model hints. Output: pinned
assessment. Run pytest tests/test_package_manufacturing_assessment.py; no I/O.
"""
from __future__ import annotations

import re

from .package_evidence_contracts import association_id, detached, evidence_id, json_bytes, object_hash, require, strict, text

ROLES = {"assembly_drawing", "assembly_instructions", "fabrication_drawing", "custom_drawing", "artwork", "drill"}


def complete_source(identity, payload, file_name):
    if not re.search(r"\.(?:txt|csv|gbr|ger|gerber|drl)$", file_name, re.I) or len(payload) > 1_000_000:
        return None
    try:
        value = payload.decode("utf-8-sig", errors="strict")
    except UnicodeError:
        return None
    if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", value):
        return None
    return {"evidenceId": evidence_id(identity), "page": None, "text": value,
            "readerVersion": "quickquote-complete-text.v1"}


def assessment_artifact(graph, classification, sources, hints):
    hints = detached(hints)
    strict(hints, {"requirements", "relationships"})
    subjects = {a["assemblyUuid"] for a in graph["assemblies"]} | {o["occurrenceId"] for o in graph["occurrences"]}
    associations = {a["associationId"]: a for a in graph["associations"]}
    source_map = {(s["evidenceId"], s["page"]): s["text"] for s in sources}

    def citations(values):
        require(type(values) is list and 1 <= len(values) <= 50, "ASSESSMENT_CITATIONS_INVALID")
        seen = set()
        for cite in values:
            strict(cite, {"evidenceId", "page", "start", "end", "text"})
            require(type(cite["evidenceId"]) is str and cite["page"] is None
                    and type(cite["start"]) is int and type(cite["end"]) is int
                    and 0 <= cite["start"] < cite["end"] <= 8_000_000, "ASSESSMENT_CITATION_INVALID")
            text(cite["text"], 2000, controls=True)
            source = source_map.get((cite["evidenceId"], cite["page"]))
            # Consumer offsets are UTF16 units, not Python Unicode code points.
            encoded = source.encode("utf-16-le") if source is not None else b""
            require(cite["end"] * 2 <= len(encoded), "ASSESSMENT_CITATION_INVALID")
            try:
                quoted = encoded[cite["start"] * 2:cite["end"] * 2].decode("utf-16-le")
            except UnicodeError:
                require(False, "ASSESSMENT_CITATION_INVALID")
            require(quoted == cite["text"] and json_bytes(cite) not in seen, "ASSESSMENT_CITATION_INVALID")
            seen.add(json_bytes(cite))

    def conflicts(values):
        require(type(values) is list and len(values) <= 20, "ASSESSMENT_CONFLICTS_INVALID")
        for value in values:
            require(text(value, controls=True) == value.strip(), "ASSESSMENT_TEXT_INVALID")

    requirements = hints["requirements"]
    require(type(requirements) is list and len(requirements) == len(subjects), "ASSESSMENT_REQUIREMENT_COVERAGE_INVALID")
    seen = set()
    for item in requirements:
        strict(item, {"subject", "requiredRoles", "determination", "rationale", "citations", "conflicts"})
        require(type(item["subject"]) is str and item["subject"] in subjects and item["subject"] not in seen, "ASSESSMENT_REQUIREMENT_COVERAGE_INVALID")
        seen.add(item["subject"])
        roles = item["requiredRoles"]
        require(type(roles) is list and len(roles) <= 6 and all(type(r) is str and r in ROLES for r in roles)
                and len(set(roles)) == len(roles), "ASSESSMENT_ROLES_INVALID")
        require(item["determination"] in {"determined", "unknown"}, "ASSESSMENT_DETERMINATION_INVALID")
        require(text(item["rationale"], 2000, controls=True) == item["rationale"].strip(), "ASSESSMENT_TEXT_INVALID")
        citations(item["citations"])
        conflicts(item["conflicts"])
    relationships = hints["relationships"]
    require(type(relationships) is list and len(relationships) == len(associations), "ASSESSMENT_RELATIONSHIP_COVERAGE_INVALID")
    seen = set()
    for item in relationships:
        if "associationId" not in item:
            strict(item, {"evidenceId", "occurrenceIds", "assemblyUuids", "scope", "role", "sharing", "citations", "conflicts"})
            identity_fields = {k: item.pop(k) for k in ("evidenceId", "occurrenceIds", "assemblyUuids", "scope")}
            require(type(identity_fields["occurrenceIds"]) is list and type(identity_fields["assemblyUuids"]) is list
                and all(type(i) is str for i in identity_fields["occurrenceIds"] + identity_fields["assemblyUuids"]), "ASSESSMENT_RELATIONSHIP_BINDING_INVALID")
            identity_fields["occurrenceIds"].sort()
            identity_fields["assemblyUuids"].sort()
            item["associationId"] = association_id(identity_fields)
        strict(item, {"associationId", "role", "sharing", "citations", "conflicts"})
        identity = item["associationId"]
        require(type(identity) is str and identity in associations and identity not in seen, "ASSESSMENT_RELATIONSHIP_COVERAGE_INVALID")
        seen.add(identity)
        require(item["role"] in ROLES and item["sharing"] in {"supported", "unknown", "not_shared"}, "ASSESSMENT_ROLE_INVALID")
        citations(item["citations"])
        conflicts(item["conflicts"])
        require(any(c["evidenceId"] == associations[identity]["evidenceId"] for c in item["citations"])
                and ((associations[identity]["scope"] == "shared") != (item["sharing"] == "not_shared")), "ASSESSMENT_RELATIONSHIP_BINDING_INVALID")
    body = {"version": "package-manufacturing-assessment.v1", "policyVersion": "source-grounded-manufacturing.v1",
        "proposalSha256": graph["proposalSha256"], "classificationSha256": classification["classificationSha256"],
        "contentSha256": object_hash(sorted(sources, key=lambda s: json_bytes([s["evidenceId"], s["page"]]))),
        **{k: graph["provenance"][k] for k in ("producer", "model", "promptSha256", "runId")},
        "requirements": detached(requirements), "relationships": detached(relationships)}
    artifact = {**body, "assessmentSha256": object_hash(body)}
    require(len(json_bytes(artifact)) <= 8 * 1024 * 1024, "ASSESSMENT_BYTE_LIMIT")
    return artifact