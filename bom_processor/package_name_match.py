"""Name-match pre-pass for the package survey (QQ #99, Nick 2026-10-14).

"As a person I would have the BOMs mapped in about 10 seconds." When every requested assembly
is named by exactly one top-level spreadsheet, a person maps those files at a glance and never
opens the hundreds of Gerber and ODB++ files inside the PCB archives. This pass does the same,
before any model call, and only when the clue is unambiguous:

- every requested assembly has exactly one root spreadsheet (not inside an archive) whose file
  name names it under the core-number rule (assembly_core.find_assembly);
- no root spreadsheet names two requested assemblies, or none of them;
- a revision written in the file name (its REV marker) does not contradict the requested one;
- every such file is one of the request's BOM documents, with bytes unlike the others;
- every root spreadsheet could be read.
Archives are never searched, even one that could not be opened (A058: two PCB archives hit the
expansion limit): a loose spreadsheet naming the assembly is the shallower, stronger clue.

Then those files become confirmed links (the same record shape the survey writes, with basis
name_match_prepass) and every other file is listed as not_surveyed: not needed for BOM choice,
never failing the package. Anything else returns None and the model survey runs unchanged.
Pure; no I/O and no model calls. Run python -m pytest tests/test_package_name_match.py.
"""
from __future__ import annotations

from typing import Any

from .assembly_core import find_assembly, revision_contradicts
from .package_survey import SPREADSHEET_KINDS, SurveyView
from .package_survey_check import assemble, cited_revision

PREPASS_VERSION = "package-name-match.v1"
BASIS = "name_match_prepass"
NOT_SURVEYED = "not_surveyed"
_SHEET_SUFFIXES = (".xls", ".xlsx", ".xlsm", ".csv")


def _finding(code: str, severity: str, rationale: str, file_ids: list[str] | None = None) -> dict[str, Any]:
    finding = {"code": code, "severity": severity, "rationale": rationale, "raised_by": "code"}
    if file_ids is not None:
        finding["file_ids"] = file_ids
    return finding


def _mapping(view: SurveyView, document_hashes: set[str]) -> dict[str, tuple[dict[str, Any], str, str | None]] | None:
    """file id -> (requested assembly, matched text, file-name revision), or None when not obvious."""
    roots = [file_id for file_id in view.ids if len(view.files[file_id].chain) == 1]
    if not view.requested or not roots:
        return None
    mapped: dict[str, tuple[dict[str, Any], str, str | None]] = {}
    for file_id in roots:
        name, entry = view.files[file_id].path, view.entries[file_id]
        if entry["kind"] not in SPREADSHEET_KINDS and not name.casefold().endswith(_SHEET_SUFFIXES):
            continue
        if entry["kind"] not in SPREADSHEET_KINDS or entry.get("unreadable"):
            return None
        named = [(item, found) for item in view.requested
                 for found in [find_assembly(item["part_number"], name)] if found is not None]
        if len(named) != 1:
            return None  # Names two requested assemblies, or none: a person or the survey decides.
        requested, found = named[0]
        revision = cited_revision(requested["part_number"], name)
        if revision_contradicts(requested["revision"], revision):
            return None
        mapped[file_id] = (requested, found, revision)
    claimed = [item["assembly_id"] for item, _, _ in mapped.values()]
    if sorted(claimed) != sorted(item["assembly_id"] for item in view.requested):
        return None  # An assembly with no file, or with two files.
    hashes = [view.files[file_id].sha256 for file_id in mapped]
    if len(set(hashes)) != len(hashes) or not set(hashes) <= document_hashes:
        return None
    return mapped


def name_match_survey(view: SurveyView, document_hashes: set[str]) -> dict[str, Any] | None:
    """A survey record with the obvious links and no model call, or None to run the model survey."""
    mapped = _mapping(view, {value.lower() for value in document_hashes})
    if mapped is None:
        return None
    records = []
    for file_id in view.ids:
        item, entry = view.files[file_id], view.entries[file_id]
        base = {"file_id": file_id, "path": item.path, "chain": list(item.chain), "sha256": item.sha256,
                "size_bytes": item.size_bytes, "kind": entry["kind"], "possibly_shared": False,
                "refused_citations": []}
        if file_id not in mapped:
            records.append({**base, "role": NOT_SURVEYED, "classification_status": NOT_SURVEYED,
                            "assembly": None, "assembly_evidence": None, "requested_assembly": None,
                            "rationale": "Not needed for BOM choice: each requested assembly is named by one "
                                         "top-level spreadsheet, so this file was listed but not surveyed.",
                            "evidence": [], "findings": []})
            continue
        requested, found, revision = mapped[file_id]
        cited = {"source": "path", "value": found}
        written = [{"source": "path", "value": revision}] if revision else []
        records.append({**base, "role": "bom", "basis": BASIS,
                        "assembly": {"name": found, "revision": revision or ""},
                        "assembly_evidence": {"name": [cited], "revision": written},
                        "requested_assembly": {"assembly_id": requested["assembly_id"], "ref": requested["ref"],
                                               "link": "confirmed", "unconfirmed_reason": None,
                                               "matched": {"text": found, "source": "path"},
                                               "matched_core": found, "evidence": [cited], "basis": BASIS},
                        "rationale": "A top-level spreadsheet whose file name names this requested assembly; "
                                     "mapped by the name-match pre-pass.",
                        "evidence": [cited],
                        "findings": [_finding("ROLE_FROM_NAME_ONLY", "warning",
                                              "The role rests on the file name, folder and type only, not on cell contents.")]})
    linked = sorted(mapped, key=lambda value: int(value[1:]))
    others = [file_id for file_id in view.ids if file_id not in mapped]
    findings = [_finding("SURVEY_NAME_MATCH", "information",
                         "Each requested assembly is named by exactly one top-level spreadsheet; those files were "
                         "mapped from their names and the model survey was not run.", linked)]
    if others:
        findings.append(_finding("SURVEY_FILES_NOT_SURVEYED", "information",
                                 "These files are not needed for BOM choice; they are listed and were not surveyed.",
                                 others))
    unopened = [file_id for file_id in view.ids if view.files[file_id].unreadable]
    if unopened:
        findings.append(_finding("SURVEY_ARCHIVE_NOT_OPENED", "information",
                                 "These archives could not be opened; their contents were not needed for BOM choice.",
                                 unopened))
    survey = assemble(view, records, findings, model_calls=0)
    survey["basis"] = BASIS
    survey["prepass"] = {"version": PREPASS_VERSION, "links": [
        {"file_id": file_id, "assembly_id": mapped[file_id][0]["assembly_id"], "matched_text": mapped[file_id][1],
         "file_name_revision": mapped[file_id][2]} for file_id in linked]}
    return survey
