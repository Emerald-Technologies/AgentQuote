"""Cross-check an agent's primary-BOM proposal for one assembly (Nick, 2026-10-08).

Inputs: the agent's proposal (which BOM is primary, what every other BOM file
is, with reasons) and the parsed rows of each BOM file of the assembly. Output:
the recorded dispositions plus a deterministic cross-check. The choice is the
agent's; code only says whether the parsed data agrees. Agreement allows the
selection without a person; disagreement, conflicts or dispositions the data
cannot confirm keep human review, with both views recorded. No I/O or model
calls. Run python -m pytest tests/test_bom_primary_selection.py.
"""
from __future__ import annotations

import re
from typing import Any, Mapping

from .bom_selection import align_boms

VERSION = "bom-primary-selection.v1"
# Every file is recorded, including ones that are not useful.
DISPOSITIONS = ("primary", "redundant", "inferior", "obsolete", "not_bom", "irrelevant")
_HASH = re.compile(r"^[0-9a-f]{64}$")

SELECTION_GUIDANCE = (
    "For one assembly, several files may each look like its BOM. Choose the one primary BOM that should be "
    "used for costing, as an experienced reviewer would: usually the most complete and current one that "
    "contains the content of the others. Label every other file: redundant (its content is contained in the "
    "primary), inferior (less complete or less reliable, not fully contained), obsolete (an older revision "
    "or superseded version), not_bom, or irrelevant. Give a short reason for each, based on the data. When "
    "unsure, still choose and add a warning finding; use a blocking finding only when no reasonable choice exists."
)


class PrimarySelectionError(ValueError):
    """Stable failure without customer values."""


def _text(value: Any, limit: int) -> bool:
    return isinstance(value, str) and 0 < len(value) <= limit


def _validate(proposal: Any, keys: set[str]) -> None:
    if (not isinstance(proposal, dict)
            or set(proposal) != {"version", "primary_fixture_sha256", "files", "rationale", "findings"}
            or proposal["version"] != VERSION
            or not _text(proposal["rationale"], 2000)
            or not isinstance(proposal["files"], list) or not isinstance(proposal["findings"], list)
            or len(proposal["findings"]) > 100):
        raise PrimarySelectionError("PRIMARY_SELECTION_INVALID")
    seen = {}
    for item in proposal["files"]:
        if (not isinstance(item, dict) or set(item) != {"fixture_sha256", "disposition", "rationale"}
                or not isinstance(item["fixture_sha256"], str) or not _HASH.fullmatch(item["fixture_sha256"])
                or item["disposition"] not in DISPOSITIONS or not _text(item["rationale"], 2000)
                or item["fixture_sha256"] in seen):
            raise PrimarySelectionError("PRIMARY_SELECTION_FILE_INVALID")
        seen[item["fixture_sha256"]] = item["disposition"]
    primaries = [key for key, value in seen.items() if value == "primary"]
    if set(seen) != keys:
        raise PrimarySelectionError("PRIMARY_SELECTION_COVERAGE_INVALID")
    if primaries != [proposal["primary_fixture_sha256"]]:
        raise PrimarySelectionError("PRIMARY_SELECTION_PRIMARY_INVALID")
    for finding in proposal["findings"]:
        if (not isinstance(finding, dict) or set(finding) != {"code", "severity", "rationale"}
                or not _text(finding["code"], 128)
                or finding["severity"] not in ("information", "warning", "blocking")
                or not _text(finding["rationale"], 2000)):
            raise PrimarySelectionError("PRIMARY_SELECTION_FINDING_INVALID")


def disposition_outcome(disposition: str, relation: str) -> str:
    """Whether one alignment relation (primary vs other file) agrees with the agent's label."""
    if relation == "conflict":
        return "conflict"
    if disposition == "redundant":
        return "agrees" if relation in ("identical", "superset") else "disagrees"
    if relation == "subset":
        # The other file contains the primary: the data suggests the opposite choice.
        return "disagrees"
    # inferior/obsolete/not_bom/irrelevant: judgments the counts cannot confirm.
    return "not_confirmable"


def cross_check(proposal: Any, boms: Mapping[str, list[Mapping[str, Any]]]) -> dict[str, Any]:
    """Record the agent's dispositions and whether the parsed BOMs agree with them.

    boms maps fixture sha256 to parsed rows for every BOM file of one assembly.
    """
    keys = set(boms)
    if not keys:
        raise PrimarySelectionError("PRIMARY_SELECTION_COVERAGE_INVALID")
    _validate(proposal, keys)
    primary = proposal["primary_fixture_sha256"]
    dispositions = {item["fixture_sha256"]: item["disposition"] for item in proposal["files"]}
    checks, reasons = [], []
    for key in sorted(keys - {primary}):
        alignment = align_boms(boms[primary], boms[key])
        disposition = dispositions[key]
        outcome = disposition_outcome(disposition, alignment["relation"])
        checks.append({"fixture_sha256": key, "disposition": disposition, "outcome": outcome,
                       "alignment": alignment})
        if outcome != "agrees":
            reasons.append(f"{outcome}:{disposition}")
    blocking = any(item["severity"] == "blocking" for item in proposal["findings"])
    if blocking:
        reasons.append("blocking_finding")
    agreed = not reasons
    return {"version": VERSION, "primary_fixture_sha256": primary, "dispositions": dispositions,
            "cross_checks": checks, "agreement": "agreed" if agreed else "needs_review",
            "review_reasons": sorted(set(reasons)), "automatic_selection_allowed": agreed,
            "proposal": proposal}
