"""Bind advisory model classifications to verified rows, never engineering approval.

Inputs: code-derived graph/rows and model judgments. Output: detached hash-bound
artifact alongside graph v1. Run pytest tests/test_package_part_classification.py.
"""
from __future__ import annotations

import re

from .package_evidence_contracts import detached, hash_value, object_hash, require, strict, text

POLICY_VERSION = "agent-inferred-part-type.v1"
TYPES = {"pcb", "custom", "commercial", "unknown"}


def advisory_signals(row: dict) -> list[dict]:
    """Nonexclusive references; absence of a hint never implies commercial.

    Missing/customer-number MPNs suggest non-commercial, not a specific subtype.
    All eight alternatives are considered; differing alternatives stay visible.
    Comparisons trim/case-fold only, never remove meaningful punctuation.
    """
    result = []
    for index, field in ((2, "description"), (8, "notes")):
        value = row["cells"][index]
        if not isinstance(value, str):
            continue
        for pattern, suggestion, rule in (
            (r"\b(?:PCB|printed circuit board)\b", "pcb", "pcb-description-reference"),
            (r"\b(?:DWG|drawing|machined|fabricated|custom)\b", "custom", "custom-description-reference"),
        ):
            match = re.search(pattern, value, re.I)
            if match:
                result.append({"rule": rule, "version": "advisory-description.v1",
                    "suggestion": suggestion, "field": field, "matchedText": match.group(0)})
    cells = row["cells"]
    mpns = [str(cells[i]).strip() for i in range(10, 25, 2)
            if cells[i] is not None and str(cells[i]).strip()]
    customer_number = str(cells[1]).strip().casefold()
    if not mpns:
        result.append({"rule": "missing-mpn", "version": "customer-mpn-reference.v1",
            "suggestion": "noncommercial", "field": "manufacturer_mpn", "matchedText": "No supplied MPN"})
    for mpn in mpns:
        if mpn.casefold() == customer_number:
            result.append({"rule": "mpn-equals-customer-part-number", "version": "customer-mpn-reference.v1",
                "suggestion": "noncommercial", "field": "manufacturer_mpn", "matchedText": mpn})
    return result


def classification_artifact(graph: dict, rows: list[dict], supplied: list[dict]) -> dict:
    require(type(supplied) is list and len(supplied) == len(graph["occurrences"]),
            "CLASSIFICATION_COVERAGE_INVALID")
    row_map = {r["occurrenceId"]: r for r in rows if r["occurrenceId"] is not None}
    require(set(row_map) == {o["occurrenceId"] for o in graph["occurrences"]},
            "CLASSIFICATION_ROW_COVERAGE_INVALID")
    evidence = {e["evidenceId"] for e in graph["evidenceObjects"]}
    output, seen = [], set()
    for item in supplied:
        strict(item, {"occurrenceId", "partType", "rationale", "evidenceIds",
                      "sourceRequiresDrawing", "sourceRequiresRevision", "conflicts"})
        identity = hash_value(item["occurrenceId"])
        require(identity in row_map and identity not in seen, "CLASSIFICATION_COVERAGE_INVALID")
        seen.add(identity)
        require(type(item["partType"]) is str and item["partType"] in TYPES, "CLASSIFICATION_TYPE_INVALID")
        require(type(item["rationale"]) is str, "CLASSIFICATION_RATIONALE_INVALID")
        rationale = text(item["rationale"].strip(), 2000, controls=True)
        ids = item["evidenceIds"]
        require(type(ids) is list and len(ids) <= 50 and all(type(i) is str for i in ids)
                and len(set(ids)) == len(ids) and set(ids) <= evidence, "CLASSIFICATION_EVIDENCE_INVALID")
        for key in ("sourceRequiresDrawing", "sourceRequiresRevision"):
            require(item[key] is None or type(item[key]) is bool, "CLASSIFICATION_REQUIREMENT_INVALID")
        conflicts = item["conflicts"]
        require(type(conflicts) is list and len(conflicts) <= 20
                and all(type(c) is str for c in conflicts), "CLASSIFICATION_CONFLICTS_INVALID")
        conflicts = [text(c.strip(), 256, controls=True) for c in conflicts]
        row = row_map[identity]
        output.append({**detached(item), "rationale": rationale, "conflicts": conflicts,
            "rowObservationSha256": object_hash(row), "heuristicSignals": advisory_signals(row)})
    provenance = graph["provenance"]
    # Match the consumer's JSON-tuple location ordering, including numeric rows.
    from .package_evidence_contracts import json_bytes
    body = {"version": "package-part-classification.v1", "policyVersion": POLICY_VERSION,
        "proposalSha256": graph["proposalSha256"],
        "rowObservationsSha256": object_hash(sorted(rows, key=lambda r:
            json_bytes([r["assemblyUuid"], r["sheet"], r["row"]]))),
        **{key: provenance[key] for key in ("producer", "model", "promptSha256", "runId")},
        "classifications": sorted(output, key=lambda c: c["occurrenceId"])}
    return detached({**body, "classificationSha256": object_hash(body)})