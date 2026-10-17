"""Known-unused files in the package survey (QuickQuote #134, #99 unit 3; Nick 2026-10-17, D9).

A person surveying a package never opens the ODB++ tree, the PCB design source, the 3D models,
CAD drawings that duplicate a PDF, or viewer files. The survey does the same: the reviewed rules
(package_file_relevance.py) mark them before any model call, they are never sent to the model, and
each is listed in the survey result as not used, with its reason (role and classification status
not_used). Nothing is dropped; the evidence mapping lists them too and a person can bring one back.
Kept-on-purpose cases (ODB++ that may be a board's only fab data, a CAD drawing with no PDF next to
it) become survey findings.

Inputs: the survey view and the survey record of the remaining files. Output: the full survey
record (package_survey_check.assemble), with a top-level not_used list. Pure; no I/O.
Run python -m pytest tests/test_package_survey_relevance.py.
"""
from __future__ import annotations

from typing import Any

from .package_file_relevance import RULES_SHA256, RULES_VERSION, classify, path_segments
from .package_survey import SurveyView
from .package_survey_check import assemble

NOT_USED = "not_used"


def known_unused(view: SurveyView) -> tuple[dict[str, dict], list[dict]]:
    """file id -> {kind, reasonCode, reason} for every known-unused file, and the rule findings."""
    ids = list(view.ids)
    results, findings = classify([path_segments(view.files[file_id].chain) for file_id in ids])
    return {file_id: label for file_id, label in zip(ids, results) if label is not None}, findings


def without(view: SurveyView, file_ids: dict[str, Any]) -> SurveyView:
    """The view of the other files, with the whole package summary kept for context."""
    keep = [file_id for file_id in view.ids if file_id not in file_ids]
    entries = {}
    for file_id in keep:
        entry = dict(view.entries[file_id])
        # Identical-bytes references point only at files still in this view.
        same = [other for other in entry.get("same_bytes_as", []) if other not in file_ids]
        entry.pop("same_bytes_as", None)
        if same:
            entry["same_bytes_as"] = same
        entries[file_id] = entry
    return SurveyView({i: view.files[i] for i in keep}, entries, {i: view.shown[i] for i in keep},
                      view.package, view.sha256, view.requested)


def _record(view: SurveyView, file_id: str, label: dict) -> dict:
    item, entry = view.files[file_id], view.entries[file_id]
    record = {"file_id": file_id, "path": item.path, "chain": list(item.chain), "sha256": item.sha256,
              "size_bytes": item.size_bytes, "kind": entry["kind"], "role": NOT_USED,
              "classification_status": NOT_USED, "assembly": None, "assembly_evidence": None,
              "possibly_shared": False, "rationale": f"Not used for quoting: {label['reason']}.",
              "evidence": [], "findings": [], "refused_citations": [],
              "not_used": {"kind": label["kind"], "reason_code": label["reasonCode"], "reason": label["reason"]}}
    if view.requested:
        record["requested_assembly"] = None
    return record


def _finding(code: str, severity: str, rationale: str, file_ids: list[str] | None = None) -> dict:
    finding = {"code": code, "severity": severity, "rationale": rationale, "raised_by": "code"}
    if file_ids is not None:
        finding["file_ids"] = file_ids
    return finding


def empty_survey(view: SurveyView) -> dict:
    """The survey of a view with no files left to survey (every file is known unused): no model call."""
    return assemble(view, [], [], model_calls=0)


def with_not_used(view: SurveyView, survey: dict, unused: dict[str, dict], rule_findings: list[dict]) -> dict:
    """The survey of the remaining files plus a not-used record for each known-unused file, re-assembled
    over the whole view. Extra keys of the remaining survey (for example the name-match basis) are kept."""
    if not unused and not rule_findings:
        return survey
    records = [record for record in survey["files"] if record["file_id"] not in unused]
    records += [_record(view, file_id, label) for file_id, label in unused.items()]
    order = {file_id: index for index, file_id in enumerate(view.ids)}
    records.sort(key=lambda record: order[record["file_id"]])
    # Package findings only: assemble adds its own code findings again over the whole view.
    findings = [item for item in survey["findings"]
                if item.get("raised_by") != "code" or item.get("code") not in {"SURVEY_FOLDERS_NOT_LISTED",
                                                                                "SAME_BYTES_DIFFERENT_ROLE",
                                                                                "SURVEY_ASSEMBLY_REVISIONS_DIFFER"}]
    if unused:
        listed = sorted(unused, key=lambda value: order[value])
        findings.append(_finding("SURVEY_FILES_NOT_USED", "information",
                                 "These files are known to be unused for quoting (ODB++, design source, 3D models, "
                                 "duplicate CAD drawings or viewer files); each is listed with its reason and was not "
                                 "surveyed.", listed))
    for item in rule_findings:
        findings.append(_finding(f"SURVEY_{item['code']}", item["severity"], item["message"]))
    result = assemble(view, records, findings, model_calls=survey["model_calls"])
    for key, value in survey.items():
        result.setdefault(key, value)
    result["not_used"] = {"rules_version": RULES_VERSION, "rules_sha256": RULES_SHA256,
                          "files": [{"file_id": file_id, "path": view.files[file_id].path,
                                     "reason_code": unused[file_id]["reasonCode"]}
                                    for file_id in sorted(unused, key=lambda value: order[value])]}
    return result
