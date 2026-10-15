"""Check the agent's stage 1 survey reply and record one survey entry per file.

Inputs: the SurveyView from package_survey.py and the agent reply for one batch.
Output: verified per-file records and code findings. Roles and assemblies are the
agent's judgment; code proves every cited cell was shown with that value, every
cited path text is in the file's path (letter case and spacing aside, recorded as
written in the source), and every file has exactly one record.
Role evidence is checked one citation at a time (QQ #182): a refused citation is
dropped and listed with a warning, and the file is refused only when no checked
citation is left for a role that needs one.
No I/O or model calls. Run python -m pytest tests/test_package_survey.py.
"""
from __future__ import annotations

import re
from typing import Any

from .assembly_core import find_assembly, revision_contradicts
from .document_role import ROLES
from .package_survey import SurveyBatch, SurveyError, SurveyView, cell_text
from .semantic import AssemblyIdentity

REPLY_VERSION = "package-survey-reply.v1"
SURVEY_VERSION = "package-survey.v1"
# Round 2: each file is also linked to a requested assembly or none, confirmed by code.
REPLY_VERSION_V2 = "package-survey-reply.v2"
SURVEY_VERSION_V2 = "package-survey.v2"
MAX_EVIDENCE = 20
MAX_IDENTITY_EVIDENCE = 5
MAX_LINK_EVIDENCE = 3
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


def _match(value: str, text: str) -> str | None:
    """The source text the agent cited: a whole token, ignoring letter case and the width of spaces.

    Returns the text exactly as written in the path or cell, so records keep the source's spelling.
    A value glued to other letters or digits ('06' inside '006') is still refused.
    """
    pattern = r"\s+".join(re.escape(part) for part in value.split())
    found = re.search(r"(?<![A-Za-z0-9])" + pattern + r"(?![A-Za-z0-9])", text, re.IGNORECASE)
    return found.group(0) if found else None


def _shortened(value: str, text: str) -> str | None:
    """A long cell cited as the view showed it, cut short and ending in '…' (QQ #182, run 003bf2c0).

    The text before the mark must start the original cell exactly; the record keeps that source text
    without the mark, so it never holds text the agent was not shown.
    """
    for mark in ("…", "..."):
        if value.endswith(mark):
            prefix = value[:-len(mark)].rstrip()
            return prefix if prefix and text.strip().startswith(prefix) else None
    return None


def _evidence(view: SurveyView, file_id: str, item: Any, *, shortened: bool = False) -> dict[str, Any]:
    """One cited item, checked against the original value (never the shortened display).

    shortened: also accept a cell's leading text cited with the view's '…' mark. Only role evidence
    allows it; an assembly name or link cut mid-word could name the wrong assembly.
    """
    if not isinstance(item, dict):
        raise SurveyError("SURVEY_EVIDENCE_RECORD_INVALID")
    if item.get("source") not in ("cell", "path"):
        raise SurveyError("SURVEY_EVIDENCE_SOURCE_INVALID")
    expected = {"source", "value"} if item["source"] == "path" else {"source", "value", "cell"}
    if set(item) != expected:
        raise SurveyError("SURVEY_EVIDENCE_FIELDS_INVALID")
    value = item["value"]
    if not isinstance(value, str):
        raise SurveyError("SURVEY_EVIDENCE_VALUE_TYPE_INVALID")
    # Surrounding spaces carry no meaning; the cited text must still be in the source.
    value = value.strip()
    if not value:
        raise SurveyError("SURVEY_EVIDENCE_VALUE_EMPTY")
    if len(value) > 256:
        raise SurveyError("SURVEY_EVIDENCE_VALUE_LIMIT")
    if item["source"] == "path":
        found = _match(value, view.files[file_id].path)
        if found is None:
            raise SurveyError("SURVEY_EVIDENCE_NOT_IN_PATH", rejected={"source": "path", "value": value})
        return {"source": "path", "value": found}
    cell = item["cell"]
    if (not isinstance(cell, dict) or set(cell) != {"sheet", "row", "column"}
            or not isinstance(cell["sheet"], str)
            or type(cell["row"]) is not int or type(cell["column"]) is not int):
        raise SurveyError("SURVEY_EVIDENCE_LOCATOR_INVALID")
    locator = {"sheet": cell["sheet"], "row": cell["row"], "column": cell["column"]}
    actual = view.shown[file_id].get((cell["sheet"], cell["row"], cell["column"]))
    if actual is None:
        # Only cells that were shown can be cited; a hidden cell is not evidence the agent saw.
        raise SurveyError("SURVEY_EVIDENCE_CELL_NOT_SHOWN", rejected={"source": "cell", "value": value, "cell": locator})
    text = cell_text(actual)
    found = value if text.strip() == value else _match(value, text)
    if found is None and shortened:
        found = _shortened(value, text)
    if found is None:
        raise SurveyError("SURVEY_EVIDENCE_VALUE_CHANGED", rejected={"source": "cell", "value": value, "cell": locator})
    return {"source": "cell", "value": found,
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


def _field(name: str, check: Any) -> Any:
    """Run one field's check; a refusal names the field so the stored result shows what was wrong."""
    try:
        return check()
    except SurveyError as exc:
        raise SurveyError(str(exc), exc.file_id, {**(exc.rejected or {}), "field": name}) from None


def _refusal(exc: SurveyError) -> dict[str, Any]:
    return {"failure_code": str(exc), **(exc.rejected or {})}


def _link(view: SurveyView, file_id: str, value: Any, assembly: dict[str, Any] | None) -> dict[str, Any] | None:
    """The agent's link to a requested assembly, confirmed only when cited text names its core number.

    An unconfirmed link is kept and shown; it never decides anything (round 2, D1/D2).
    """
    if value is None:
        return None
    refs = {item["ref"]: item for item in view.requested}
    if (not isinstance(value, dict) or set(value) != {"ref", "matched_core", "evidence"}
            or value["ref"] not in refs or not _text(value["matched_core"], 100)
            or not isinstance(value["evidence"], list) or not 1 <= len(value["evidence"]) <= MAX_LINK_EVIDENCE):
        raise SurveyError("SURVEY_REQUESTED_ASSEMBLY_INVALID")
    requested = refs[value["ref"]]
    evidence = [_evidence(view, file_id, item) for item in value["evidence"]]
    return _confirm(requested, value["ref"], value["matched_core"], evidence, assembly)


def _contained_link(view: SurveyView, value: Any, assembly: dict[str, Any] | None) -> dict[str, Any] | None:
    """A link whose own citation was refused (round 3, A036): keep the agent's ref when it is valid
    and confirm only from the file's other verified citations; never refuse the file."""
    refs = {item["ref"]: item for item in view.requested}
    if not isinstance(value, dict) or value.get("ref") not in refs:
        return None
    core = value.get("matched_core") if _text(value.get("matched_core"), 100) else None
    link = _confirm(refs[value["ref"]], value["ref"], core, [], assembly)
    if link["link"] == "unconfirmed":
        link["unconfirmed_reason"] = "citation_refused"
    return link


_REVISION_MARKER = re.compile(r"(?i)(?<![a-z])rev(?:ision)?(?![a-z])[\s._:#-]*([a-z0-9]+(?:\.[0-9]+)*)")


def cited_revision(part_number: str, text: Any) -> Any:
    """The revision a cited text gives (QQ A057, Nick 2026-10-14).

    A plain revision ('02') is used as written. A text that also names the part number, such as a
    file path or a title ('BOM-0100-04201-REV-02.xlsx'), gives only the token after its REV or
    Revision marker; with no marker its revision is unknown (None), never a contradiction.
    """
    if not isinstance(text, str) or find_assembly(part_number, text) is None:
        return text
    match = _REVISION_MARKER.search(text)
    return match.group(1) if match else None


def _confirm(requested: dict[str, Any], ref: str, core: str | None, evidence: list[dict[str, Any]],
             assembly: dict[str, Any] | None) -> dict[str, Any]:
    matched = None
    for cited in evidence + (assembly["name"] if assembly is not None else []):
        found = find_assembly(requested["part_number"], cited["value"])
        if found is not None:
            matched = {"text": found, **{key: cited[key] for key in ("source", "cell") if key in cited}}
            break
    revision = assembly["revision"][0]["value"] if assembly is not None and assembly["revision"] else None
    differs = revision_contradicts(requested["revision"], cited_revision(requested["part_number"], revision))
    confirmed = matched is not None and not differs
    return {"assembly_id": requested["assembly_id"], "ref": ref,
            "link": "confirmed" if confirmed else "unconfirmed",
            "unconfirmed_reason": None if confirmed else (
                "revision_differs" if matched is not None else "number_not_found"),
            "matched": matched, "matched_core": core, "evidence": evidence}


def _code_finding(code: str, severity: str, rationale: str) -> dict[str, Any]:
    return {"code": code, "severity": severity, "rationale": rationale, "raised_by": "code"}


def _key(value: str) -> str:
    return AssemblyIdentity(value).normalized()[0]


def _record(view: SurveyView, file_id: str, proposal: Any) -> dict[str, Any]:
    keys = {"file_id", "role", "assembly", "possibly_shared", "rationale", "evidence", "findings"}
    if view.requested:
        keys.add("requested_assembly")
    if (not isinstance(proposal, dict) or set(proposal) != keys
            or proposal["role"] not in ROLES or type(proposal["possibly_shared"]) is not bool
            or not _text(proposal["rationale"], 1000)
            or not isinstance(proposal["evidence"], list) or len(proposal["evidence"]) > MAX_EVIDENCE):
        raise SurveyError("SURVEY_RECORD_INVALID")
    if proposal["role"] != "other" and not proposal["evidence"]:
        raise SurveyError("SURVEY_ROLE_EVIDENCE_REQUIRED", rejected={"field": "evidence"})
    evidence, refused = _role_evidence(view, file_id, proposal["role"], proposal["evidence"])
    evidence_refused = bool(refused)
    link = None
    if view.requested:
        # Round 3 (A036): with requested assemblies the provisional assembly and the link decide no
        # role, so a refused citation there drops only that part; the file keeps its classification.
        try:
            assembly = _assembly(view, file_id, proposal["assembly"])
        except SurveyError as exc:
            assembly = None
            refused.append({"field": "assembly", **_refusal(exc)})
        try:
            link = _link(view, file_id, proposal["requested_assembly"], assembly)
        except SurveyError as exc:
            link = _contained_link(view, proposal["requested_assembly"], assembly)
            refused.append({"field": "requested_assembly", **_refusal(exc)})
    else:
        assembly = _field("assembly", lambda: _assembly(view, file_id, proposal["assembly"]))
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
    if evidence_refused:
        findings.append(_code_finding("SURVEY_EVIDENCE_CITATION_REFUSED", "warning",
                                      "A cited value for the file's role did not match the file; that citation was "
                                      "dropped and the file keeps its classification on the citations that matched."))
    if any(item["field"] != "evidence" for item in refused):
        findings.append(_code_finding("SURVEY_LINK_CITATION_REFUSED", "warning",
                                      "A citation for the file's assembly or requested-assembly link was refused; "
                                      "only that part was dropped and the file keeps its classification."))
    if link is not None and link["link"] == "unconfirmed":
        findings.append(_code_finding("SURVEY_MAPPING_UNCONFIRMED", "warning",
                                      "The file was linked to a requested assembly, but no cited text names its "
                                      "part number with an agreeing revision; the link decides nothing."))
    identity = None
    if assembly is not None:
        identity = {"name": assembly["name"][0]["value"],
                    "revision": assembly["revision"][0]["value"] if assembly["revision"] else ""}
    record = {"file_id": file_id, "path": item.path, "chain": list(item.chain), "sha256": item.sha256,
              "size_bytes": item.size_bytes, "kind": entry["kind"], "role": role,
              "assembly": identity, "assembly_evidence": assembly,
              "possibly_shared": proposal["possibly_shared"], "rationale": proposal["rationale"],
              "evidence": evidence, "findings": findings}
    if view.requested:
        record["requested_assembly"] = link
        record["refused_citations"] = refused
    elif refused:
        record["refused_citations"] = refused
    return record


def _role_evidence(view: SurveyView, file_id: str, role: str,
                   items: list[Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Role citations checked one at a time: those that match, and the refused ones (QQ #182).

    A refused citation (a sheet name cited as a cell's text, a changed value, a cell not shown) is
    dropped. A role other than 'other' with no matching citation left is refused with the first
    citation's own code, as before, so the retry tells the agent what was wrong.
    """
    evidence, refused, first = [], [], None
    for item in items:
        try:
            evidence.append(_evidence(view, file_id, item, shortened=True))
        except SurveyError as exc:
            first = first or exc
            refused.append({"field": "evidence", **_refusal(exc)})
    if role != "other" and not evidence:
        raise SurveyError(str(first), first.file_id, {**(first.rejected or {}), "field": "evidence"})
    return evidence, refused


def verify_batch(view: SurveyView, batch: SurveyBatch, reply: Any) -> tuple[list[dict], list[dict]]:
    """Records for exactly the batch's files, plus package-level agent findings; any refusal raises."""
    records, findings, failed = verify_batch_files(view, batch, reply)
    if failed:
        raise next(iter(failed.values()))
    return records, findings


def verify_batch_files(view: SurveyView, batch: SurveyBatch,
                       reply: Any) -> tuple[list[dict], list[dict], dict[str, SurveyError]]:
    """Records for the files that pass, package-level agent findings, and each refused file's error.

    A malformed reply or wrong file coverage still refuses the whole reply; one refused record
    refuses only that file (plan 8.8 step 7, A032 diagnosis).
    """
    if (not isinstance(reply, dict) or set(reply) != {"version", "files", "findings"}
            or reply["version"] != (REPLY_VERSION_V2 if view.requested else REPLY_VERSION)
            or not isinstance(reply["files"], list)):
        raise SurveyError("SURVEY_REPLY_INVALID")
    proposals: dict[str, Any] = {}
    for proposal in reply["files"]:
        file_id = proposal.get("file_id") if isinstance(proposal, dict) else None
        if not isinstance(file_id, str) or file_id in proposals:
            raise SurveyError("SURVEY_COVERAGE_INVALID")
        proposals[file_id] = proposal
    if set(proposals) != set(batch.file_ids):
        raise SurveyError("SURVEY_COVERAGE_INVALID")
    findings = [{**item, "raised_by": "agent"} for item in _findings(reply["findings"])]
    records, failed = [], {}
    for file_id in batch.file_ids:
        try:
            records.append(_record(view, file_id, proposals[file_id]))
        except SurveyError as exc:
            # The survey id (F1...) is not customer data; the retry and the record can name the file.
            failed[file_id] = SurveyError(str(exc), file_id, exc.rejected)
    return records, findings, failed


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
    survey = {"version": SURVEY_VERSION_V2 if view.requested else SURVEY_VERSION, "view_sha256": view.sha256,
              "model_calls": model_calls, "files": [by_id[file_id] for file_id in view.ids],
              "assemblies": summary, "unassigned": unassigned, "findings": findings,
              "has_blocking_findings": blocking}
    if view.requested:
        survey["requested_assemblies"] = [_requested_group(item, [by_id[file_id] for file_id in view.ids])
                                          for item in view.requested]
    return survey


def _requested_group(requested: dict[str, Any], records: list[dict]) -> dict[str, Any]:
    """Files whose link to this requested assembly code confirmed; unconfirmed links group nothing."""
    linked = [record for record in records
              if (record.get("requested_assembly") or {}).get("link") == "confirmed"
              and record["requested_assembly"]["assembly_id"] == requested["assembly_id"]]
    return {"assembly_id": requested["assembly_id"], "ref": requested["ref"],
            "part_number": requested["part_number"], "revision": requested["revision"],
            "files": {role: [record["file_id"] for record in linked if record["role"] == role] for role in ROLES}}
