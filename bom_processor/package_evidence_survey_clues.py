"""Survey clues for the evidence producer (Nick 2026-10-12, multi-assembly labor plan step 1).

The package survey (stage 1) already read every file and named an assembly for most. The v6
evidence producer reads the same package; giving it the survey's reading as a clue keeps the two
passes consistent and lets later survey improvements carry over. A clue is never proof: the model
may disagree, and code only uses it to place a file into an assembly's call and to allow a
not-text-verified link for a file that cannot be quoted (decision 6).

Inputs: storage and the BOM plan id (loader); the producer's inventory, root names and the survey
records (matching). A survey record matches an inventory file only on the same full path and the
same file hash; otherwise the file has no clue. The loader never blocks the producer: anything
missing or unreadable means no clues.
Run python -m pytest tests/test_package_evidence_survey_clues.py.
"""
from __future__ import annotations

from typing import Any

LINKS = ("confirmed", "unconfirmed")


def load_survey_files(storage: Any, bom_plan_id: str) -> list[dict] | None:
    """The survey records bound to this BOM plan, or None when the plan has no readable survey."""
    try:
        from .bom_package_choice import bound_state
        from .package_resolution import ParentPackageService
        plan = ParentPackageService(storage)._plan(bom_plan_id)
        reference = plan.get("package_choice")
        if not reference:
            return None
        files = bound_state(storage, reference)["result"]["survey"]["files"]
        return files if isinstance(files, list) else None
    except Exception:  # A clue source, never a gate: the producer runs as before without it.
        return None


def _leaf_sha(item: dict) -> str:
    chain = item.get("archiveChain") or []
    return chain[-1]["sha256"] if chain else item["sha256"]


def _inventory_path(item: dict, names: dict[str, str]) -> str:
    return "/".join([names[item["documentId"]], *(step["memberPath"] for step in item.get("archiveChain") or [])])


def match_clues(inventory: list[dict], names: dict[str, str], survey_files: list[dict] | None,
                assembly_ids: set[str]) -> dict[str, dict]:
    """evidenceId -> {"assembly": uuid | None, "link": confirmed | unconfirmed | none, "kind": str}."""
    if not survey_files:
        return {}
    by_path: dict[str, dict] = {}
    for record in survey_files:
        if isinstance(record, dict) and isinstance(record.get("path"), str):
            # A path the survey saw twice is not a reliable key.
            by_path[record["path"]] = None if record["path"] in by_path else record
    clues = {}
    for item in inventory:
        record = by_path.get(_inventory_path(item, names))
        if not record or record.get("sha256") != _leaf_sha(item):
            continue
        requested = record.get("requested_assembly") or {}
        assembly = requested.get("assembly_id")
        link = requested.get("link")
        if assembly not in assembly_ids or link not in LINKS:
            assembly, link = None, "none"
        clues[item["evidenceId"]] = {"assembly": assembly, "link": link, "kind": str(record.get("kind") or "")[:64]}
    return clues
