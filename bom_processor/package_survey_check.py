"""Check the agent's stage 1 survey reply and record one survey entry per file.

Inputs: the SurveyView from package_survey.py and the agent reply for one batch.
Output: verified per-file records and code findings. Roles and assemblies are the
agent's judgment; code proves every cited cell was shown with that value, every
cited path text is in the file's path, and every file has exactly one record.
No I/O or model calls. Run python -m pytest tests/test_package_survey.py.
"""
from __future__ import annotations

import re
from typing import Any

from .assembly_association import _within_cell
from .document_role import ROLES
from .package_survey import SurveyBatch, SurveyError, SurveyView, cell_text
from .semantic import AssemblyIdentity

REPLY_VERSION = "package-survey-reply.v1"
SURVEY_VERSION = "package-survey.v1"
MAX_EVIDENCE = 20
MAX_IDENTITY_EVIDENCE = 5
SEVERITIES = ("information", "warning", "blocking")


def _text(value: Any, limit: int) -> bool:
    return isinstance(value, str) and 0 < len(value) <= limit and value == value.strip()


def _findings(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > 50:
        raise SurveyError("SURVEY_FINDING_INVALID")
    for finding in value:
        if (not isinstance(finding, dict) or set(finding) != {"code", "severity", "rationale"}
                or not isinstance(finding["code"], str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", finding["code"])
                or finding["severity"] not in SEVERITIES or not _text(finding["rationale"], 1000)):
            raise SurveyError("SURVEY_FINDING_INVALID")
    return [dict(finding) for finding in value]


def _evidence(view: SurveyView, file_id: str, item: Any) -> dict[str, Any]:
    """One cited item, checked against the original value (never the shortened display)."""
    if not isinstance(item, dict) or item.get("source") not in ("cell", "path"):
        raise SurveyError("SURVEY_EVIDENCE_INVALID")
    if item["source"] == "path":
        if set(item) != {"source", "value"} or not _text(item["value"], 256):
            raise SurveyError("SURVEY_EVIDENCE_INVALID")
        if not _within_cell(item["value"], view.files[file_id].path):
            raise SurveyError("SURVEY_EVIDENCE_NOT_IN_PATH")
        return {"source": "path", "value": item["value"]}
    if set(item) != {"source", "value", "cell"} or not _text(item["value"], 256):
        raise SurveyError("SURVEY_EVIDENCE_INVALID")
    cell = item["cell"]
    if (not isinstance(cell, dict) or set(cell) != {"sheet", "row", "column"}
            or not isinstance(cell["sheet"], str)
            or type(cell["row"]) is not int or type(cell["column"]) is not int):
        raise SurveyError("SURVEY_EVIDENCE_LOCATOR_INVALID")
    actual = view.shown[file_id].get((cell["sheet"], cell["row"], cell["column"]))
    if actual is None:
        # Only cells that were shown can be cited; a hidden cell is not evidence the agent saw.
        raise SurveyError("SURVEY_EVIDENCE_CELL_NOT_SHOWN")
    text = cell_text(actual)
    if text.strip() != item["value"] and not _within_cell(item["value"], text):
        raise SurveyError("SURVEY_EVIDENCE_VALUE_CHANGED")
    return {"source": "cell", "value": item["value"],
            "cell": {"sheet": cell["sheet"], "row": cell["row"], "column": cell["column"]}}


def _assembly(view: SurveyView, file_id: str, value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if (not isinstance(value, dict) or set(value) != {"name", "revision"}
            or not isinstance(value["name"], list) or not 1 <= len(value["name"]) <= MAX_IDENTITY_EVIDENCE
            or not isinstance(value["revision"], list) or len(value["revision"]) > MAX_IDENTITY_EVIDENCE):
        raise SurveyError("SURVEY_ASSEMBLY_INVALID")
    name = [_evidence(view, file_id, item) for item in value["name"]]
    revision = [_evidence(view, file_id, item) for item in value["revision"]]
    return {"name": name, "revision": revision}


def _code_finding(code: str, severity: str, rationale: str) -> dict[str, Any]:
    return {"code": code, "severity": severity, "rationale": rationale, "raised_by": "code"}


def _key(value: str) -> str:
    return AssemblyIdentity(value).normalized()[0]


def _record(view: SurveyView, file_id: str, proposal: Any) -> dict[str, Any]:
    if (not isinstance(proposal, dict) or set(proposal) != {
            "file_id", "role", "assembly", "possibly_shared", "rationale", "evidence", "findings"}
            or proposal["role"] not in ROLES or type(proposal["possibly_shared"]) is not bool
            or not _text(proposal["rationale"], 1000)
            or not isinstance(proposal["evidence"], list) or len(proposal["evidence"]) > MAX_EVIDENCE):
        raise SurveyError("SURVEY_RECORD_INVALID")
    evidence = [_evidence(view, file_id, item) for item in proposal["evidence"]]
    if proposal["role"] != "other" and not evidence:
        raise SurveyError("SURVEY_ROLE_EVIDENCE_REQUIRED")
    assembly = _assembly(view, file_id, proposal["assembly"])
    findings = [{**item, "raised_by": "agent"} for item in _findings(proposal["findings"])]
    item = view.files[file_id]
    entry = view.entries[file_id]
    role = proposal["role"]
    if assembly is None:
        # Plan 8.2-5: a file that names no assembly is a warning, never a block.
        findings.append(_code_finding("SURVEY_ASSEMBLY_NOT_IDENTIFIED", "warning",
                                      "No assembly is identified for this file."))
    else:
        sources = {_key(cited["value"]): cited["source"] for cited in assembly["name"]}
        cell_names = {key for key, source in sources.items() if source == "cell"}
        path_names = {key for key, source in sources.items() if source == "path"}
        # Contents decide (plan 8.2-12); a different identifier in the name or folder is reported.
        if cell_names and path_names and not cell_names & path_names:
            findings.append(_code_finding("NAME_CONTENT_DISAGREE", "warning",
                                          "The assembly cited from the file name or folder differs from the one in the cells."))
    if role != "other" and not any(cited["source"] == "cell" for cited in evidence):
        findings.append(_code_finding("ROLE_FROM_NAME_ONLY", "warning",
                                      "The role rests on the file name, folder and type only, not on cell contents."))
    for flag, code, text in (
            ("view_limited", "SURVEY_VIEW_LIMITED", "Only part of the top rows fitted the survey view."),
            ("macros_present_not_opened", "SURVEY_MACROS_NOT_OPENED", "The workbook holds macros; they were not opened."),
            ("unreadable", "SURVEY_FILE_UNREADABLE", "The file could not be opened; its role rests on its name and type.")):
        if entry.get(flag):
            findings.append(_code_finding(code, "information", text))
    identity = None
    if assembly is not None:
        identity = {"name": assembly["name"][0]["value"],
                    "revision": assembly["revision"][0]["value"] if assembly["revision"] else ""}
    return {"file_id": file_id, "path": item.path, "chain": list(item.chain), "sha256": item.sha256,
            "size_bytes": item.size_bytes, "kind": entry["kind"], "role": role,
            "assembly": identity, "assembly_evidence": assembly,
            "possibly_shared": proposal["possibly_shared"], "rationale": proposal["rationale"],
            "evidence": evidence, "findings": findings}


def verify_batch(view: SurveyView, batch: SurveyBatch, reply: Any) -> tuple[list[dict], list[dict]]:
    """Records for exactly the batch's files, plus package-level agent findings."""
    if (not isinstance(reply, dict) or set(reply) != {"version", "files", "findings"}
            or reply["version"] != REPLY_VERSION or not isinstance(reply["files"], list)):
        raise SurveyError("SURVEY_REPLY_INVALID")
    proposals: dict[str, Any] = {}
    for proposal in reply["files"]:
        file_id = proposal.get("file_id") if isinstance(proposal, dict) else None
        if not isinstance(file_id, str) or file_id in proposals:
            raise SurveyError("SURVEY_COVERAGE_INVALID")
        proposals[file_id] = proposal
    if set(proposals) != set(batch.file_ids):
        raise SurveyError("SURVEY_COVERAGE_INVALID")
    records = [_record(view, file_id, proposals[file_id]) for file_id in batch.file_ids]
    return records, [{**item, "raised_by": "agent"} for item in _findings(reply["findings"])]


def assemble(view: SurveyView, records: list[dict], package_findings: list[dict],
             *, model_calls: int) -> dict[str, Any]:
    """One survey record per file, provisional assemblies, and cross-file code findings."""
    by_id = {record["file_id"]: record for record in records}
    if len(by_id) != len(records) or set(by_id) != set(view.ids):
        raise SurveyError("SURVEY_COVERAGE_INVALID")
    findings = list(package_findings)
    if view.package.get("folders_not_listed"):
        findings.append(_code_finding("SURVEY_FOLDERS_NOT_LISTED", "information",
                                      "Some folders did not fit the package summary; every file was still classified."))
    # Identical bytes cannot hold different contents; a different verdict is reported.
    for record in records:
        for other in view.entries[record["file_id"]].get("same_bytes_as", []):
            if other > record["file_id"] and by_id[other]["role"] != record["role"]:
                findings.append(_code_finding("SAME_BYTES_DIFFERENT_ROLE", "warning",
                                              f"{record['file_id']} and {other} have identical bytes but different roles."))
    assemblies: dict[str, dict[str, Any]] = {}
    for record in records:
        if record["assembly"] is None:
            continue
        key = _key(record["assembly"]["name"])
        group = assemblies.setdefault(key, {"name": record["assembly"]["name"], "revisions": set(),
                                            "files": {role: [] for role in ROLES}})
        if record["assembly"]["revision"]:
            group["revisions"].add(record["assembly"]["revision"])
        group["files"][record["role"]].append(record["file_id"])
    summary = [{"name": group["name"], "revisions": sorted(group["revisions"]), "files": group["files"]}
               for _, group in sorted(assemblies.items())]
    for group in summary:
        if len(group["revisions"]) > 1:
            findings.append(_code_finding("SURVEY_ASSEMBLY_REVISIONS_DIFFER", "information",
                                          "Files of one assembly cite different revisions."))
    unassigned = [record["file_id"] for record in records if record["assembly"] is None]
    blocking = any(item["severity"] == "blocking" for item in findings) or any(
        item["severity"] == "blocking" for record in records for item in record["findings"])
    return {"version": SURVEY_VERSION, "view_sha256": view.sha256, "model_calls": model_calls,
            "files": [by_id[file_id] for file_id in view.ids], "assemblies": summary,
            "unassigned": unassigned, "findings": findings, "has_blocking_findings": blocking}
