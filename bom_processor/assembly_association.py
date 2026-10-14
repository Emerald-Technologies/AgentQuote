"""Verify source-backed agent assembly proposals without filename inference.

Inputs: original spreadsheet bytes and a proposal with cell references. Output:
byte-bound association evidence, not semantic approval. Run association pytest.
"""
from __future__ import annotations

import re
from hashlib import sha256
from typing import Any

from .interpretation_inventory import extract_inventory
from .semantic import AssemblyIdentity

VERSION = "source-assembly-association.v1"


class AssociationError(ValueError):
    """Stable failure without customer values."""


def _within_cell(value: str, cell_text: str) -> bool:
    """True when value appears in the cell as a whole token: not glued to letters/digits.

    Accepts 'Assembly Part:006-002022-01' or 'Revision:013' for the cited value;
    refuses '06' inside '006' and other partial tokens.
    """
    pattern = r"(?<![A-Za-z0-9])" + re.escape(value) + r"(?![A-Za-z0-9])"
    return re.search(pattern, cell_text) is not None


def verify_association(source: bytes, extension: str, proposal: Any) -> dict[str, Any]:
    """Check exact source values; the declaration role remains an agent decision."""
    if (not isinstance(proposal, dict) or set(proposal) != {
            "version", "source_sha256", "name", "revision", "rationale", "findings"}
            or proposal["version"] != VERSION
            or proposal["source_sha256"] != sha256(source).hexdigest()
            or not isinstance(proposal["rationale"], str)
            or not 0 < len(proposal["rationale"]) <= 2000
            or not isinstance(proposal["findings"], list)
            or len(proposal["findings"]) > 100):
        raise AssociationError("ASSOCIATION_BINDING_INVALID")
    inventory = extract_inventory(source, extension)
    within_cell: list[str] = []

    def value(field: str) -> str:
        ref = proposal[field]
        if field == "revision" and ref is None:
            return ""
        if (not isinstance(ref, dict) or set(ref) != {"value", "cell"}
                or not isinstance(ref["value"], str) or not 0 < len(ref["value"]) <= 256
                or ref["value"] != ref["value"].strip()):
            raise AssociationError("ASSOCIATION_VALUE_INVALID")
        cell = ref["cell"]
        if (not isinstance(cell, dict) or set(cell) != {"sheet", "row", "column"}
                or not isinstance(cell["sheet"], str)
                or type(cell["row"]) is not int or type(cell["column"]) is not int):
            raise AssociationError("ASSOCIATION_LOCATOR_INVALID")
        actual = inventory.cells.get((cell["sheet"], cell["row"], cell["column"]))
        if actual is None:
            raise AssociationError("ASSOCIATION_SOURCE_VALUE_CHANGED")
        if str(actual).strip() != ref["value"]:
            if not _within_cell(ref["value"], str(actual)):
                raise AssociationError("ASSOCIATION_SOURCE_VALUE_CHANGED")
            within_cell.append(field)
        return ref["value"]

    name, revision = value("name"), value("revision")
    normalized = AssemblyIdentity(name, revision).normalized()
    if not normalized[0]:
        raise AssociationError("ASSOCIATION_VALUE_INVALID")
    blocking = False
    for finding in proposal["findings"]:
        if (not isinstance(finding, dict) or set(finding) != {"code", "severity", "rationale"}
                or not isinstance(finding["code"], str) or not 0 < len(finding["code"]) <= 128
                or finding["severity"] not in ("information", "warning", "blocking")
                or not isinstance(finding["rationale"], str)
                or not 0 < len(finding["rationale"]) <= 2000):
            raise AssociationError("ASSOCIATION_FINDING_INVALID")
        blocking |= finding["severity"] == "blocking"
    result = {"version": VERSION, "source_sha256": inventory.sha256,
              "assembly_identity": {"name": normalized[0], "revision": normalized[1]},
              "source_values_verified": True, "has_blocking_findings": blocking,
              "semantic_acceptance": "not_evaluated", "proposal": proposal}
    if within_cell:
        # Only present for part-of-cell citations, so whole-cell evidence is unchanged.
        result["within_cell_fields"] = within_cell
    return result