"""Name-match pre-pass for the package survey (QQ #99, Nick 2026-10-14; any depth, D7 2026-10-17).

"As a person I would have the BOMs mapped in about 10 seconds." When every requested assembly
is plainly named by one spreadsheet, a person maps those files at a glance and never opens the
hundreds of Gerber and ODB++ files inside the PCB archives. This pass does the same, before any
model call, and only when the clue is unambiguous. Clues, not rules:

- Spreadsheets at any depth are looked at: loose files, files in folders and files inside nested
  zips. The whole path is the clue text: the file name, then each folder and archive name
  ("<PN>", "<PN> Rev B", "<PN>_RevB", "ASSY <PN>", any common separator), under the core-number
  rule (assembly_core.find_assembly).
- A path whose file name and folders name different requested assemblies, or one name naming
  two, is a conflict: the survey and a person decide (D7). So is a written revision (REV marker
  in the file name or a folder) that contradicts the requested one.
- Placement, test and fab data (package_survey_rank clues: pick and place, ICT, Gerber, ...) is
  never this pass's BOM unless its name or headings read like a BOM.
- Shallow beats deep: for each assembly only its shallowest candidates count; a deeper file
  naming it is ranked lower and listed. At that depth one candidate wins; of several, those whose
  name or headings read like a BOM are all linked as candidates (a customer BOM and a modified
  BOM, #99 blocker 7) and the package choice step picks the primary, as after a survey; when none
  reads like a BOM, the survey runs.
- A spreadsheet naming no requested assembly no longer cancels the pass by itself; it is listed
  as not matched. It still cancels when it looks like a BOM (name, headings, or cells naming a
  requested assembly) and is no deeper than a matched BOM: it may be a sub-assembly's or a
  combined BOM, which the survey and a person decide. A conflicting file cancels the same way.
- Each linked file is one of the request's BOM documents, with bytes unlike the other
  assemblies' (identical copies count once; the shallowest copy is linked).
- Empty or header-only spreadsheets are passed over (QQ #99 D5); an unreadable one counts as a
  doubt only when it names a requested assembly or is named like a BOM.

Then those files become confirmed links (the same record shape the survey writes, with basis
name_match_prepass) and every other file is listed as not_surveyed: not needed for BOM choice,
never failing the package. Anything else returns None and the model survey runs unchanged.
Pure; no I/O and no model calls. Run python -m pytest tests/test_package_name_match.py.
"""
from __future__ import annotations

from typing import Any, NamedTuple

from .assembly_core import find_assembly, revision_contradicts
from .package_survey import SPREADSHEET_KINDS, SurveyView, nothing_to_read
from .package_survey_check import assemble, cited_revision
from .package_survey_rank import rank_files

# v2 (D7, 2026-10-17): spreadsheets at any depth; folder and archive names are clues.
PREPASS_VERSION = "package-name-match.v2"
BASIS = "name_match_prepass"
NOT_SURVEYED = "not_surveyed"
_SHEET_SUFFIXES = (".xls", ".xlsx", ".xlsm", ".csv")
_BOM_CLUES = frozenset({"bom_name", "bom_headings"})


class PathMatch(NamedTuple):
    """One requested assembly a path names: the text as written, any written revision, and where."""
    requested: dict[str, Any]
    found: str
    revision: str | None
    matched_in: str  # "file_name" or "folder" (a folder or archive name)


def _finding(code: str, severity: str, rationale: str, file_ids: list[str] | None = None) -> dict[str, Any]:
    finding = {"code": code, "severity": severity, "rationale": rationale, "raised_by": "code"}
    if file_ids is not None:
        finding["file_ids"] = file_ids
    return finding


def _depth(path: str) -> int:
    return path.count("/")


def _stem(segment: str) -> str:
    return segment[:-4] if segment.casefold().endswith(".zip") else segment


def path_names(requested: tuple[dict[str, Any], ...] | list[dict[str, Any]], path: str
               ) -> tuple[list[PathMatch], bool]:
    """Requested assemblies a path names (one entry each), and whether its clues conflict.

    Segments are read from the file name outward. A conflict is two different assemblies named
    anywhere on the path, or a written revision that contradicts the requested one.
    """
    hits: dict[str, list[PathMatch]] = {}
    conflict = False
    for index, segment in enumerate(reversed(path.split("/"))):
        text = segment if index == 0 else _stem(segment)
        for item in requested:
            found = find_assembly(item["part_number"], text)
            if found is None:
                continue
            revision = cited_revision(item["part_number"], text)
            if revision_contradicts(item["revision"], revision):
                conflict = True
            hits.setdefault(item["assembly_id"], []).append(
                PathMatch(item, found, revision, "file_name" if index == 0 else "folder"))
    if len(hits) > 1:
        conflict = True
    # The nearest segment gives the matched text; the nearest written revision is kept.
    matches = [found[0]._replace(revision=next((match.revision for match in found if match.revision), None))
               for found in hits.values()]
    return matches, conflict


def _mapping(view: SurveyView, document_hashes: set[str]) -> dict[str, PathMatch] | None:
    """file id -> how its path names its requested assembly, or None when not obvious."""
    if not view.requested:
        return None
    clues = {item["file_id"]: set(item["clues"]) for item in rank_files(view)}
    candidates: dict[str, list[tuple[str, PathMatch, bool]]] = {}
    doubtful: list[str] = []
    for file_id in view.ids:
        path, entry = view.files[file_id].path, view.entries[file_id]
        if entry["kind"] not in SPREADSHEET_KINDS and not path.casefold().endswith(_SHEET_SUFFIXES):
            continue
        named, conflict = path_names(view.requested, path)
        looks_like_bom = bool(clues[file_id] & _BOM_CLUES)
        if entry["kind"] not in SPREADSHEET_KINDS or entry.get("unreadable"):
            if named or "bom_name" in clues[file_id]:
                doubtful.append(file_id)  # It could be the BOM, and it cannot be read here.
            continue
        if nothing_to_read(view, file_id):
            continue  # No parts to read: never the BOM, and no reason to stop the pre-pass (D5).
        if "non_bom_context" in clues[file_id] and not looks_like_bom:
            continue  # Placement, test or fab data: listed, never this pass's BOM.
        if conflict:
            doubtful.append(file_id)  # Its path names different assemblies or revisions: a person decides.
            continue
        if not named:
            if looks_like_bom or "names_requested_in_cells" in clues[file_id]:
                doubtful.append(file_id)  # A BOM of something: maybe a sub-assembly's or a combined BOM.
            continue  # Names no requested assembly: not matched, listed.
        candidates.setdefault(named[0].requested["assembly_id"], []).append((file_id, named[0], looks_like_bom))
    mapped: dict[str, PathMatch] = {}
    for requested in view.requested:
        found = candidates.get(requested["assembly_id"], [])
        if not found:
            return None  # An assembly with no file.
        top = min(_depth(view.files[file_id].path) for file_id, _, _ in found)
        level: dict[str, tuple[str, PathMatch, bool]] = {}
        for candidate in sorted(found, key=lambda item: view.files[item[0]].path):
            if _depth(view.files[candidate[0]].path) == top:
                level.setdefault(view.files[candidate[0]].sha256, candidate)  # Identical copies count once.
        choices = list(level.values())
        if len(choices) > 1:
            choices = [candidate for candidate in choices if candidate[2]]
        if not choices:
            return None  # Several files, none reading like a BOM: the survey and a person decide.
        # Several BOM-like files (a customer BOM and a modified BOM): all are linked as candidates,
        # and the package choice step picks the primary as it does after a survey (#99 blocker 7).
        for file_id, match, _ in choices:
            mapped[file_id] = match
    deepest = max(_depth(view.files[file_id].path) for file_id in mapped)
    if any(_depth(view.files[file_id].path) <= deepest for file_id in doubtful):
        return None  # A doubt no deeper than a matched BOM: a person would stop and look.
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
                            "rationale": "Not needed for BOM choice: each requested assembly is named by its "
                                         "spreadsheets' names or folders, so this file was listed but not surveyed.",
                            "evidence": [], "findings": []})
            continue
        requested, found, revision, _ = mapped[file_id]
        cited = {"source": "path", "value": found}
        written = [{"source": "path", "value": revision}] if revision else []
        records.append({**base, "role": "bom", "basis": BASIS,
                        "assembly": {"name": found, "revision": revision or ""},
                        "assembly_evidence": {"name": [cited], "revision": written},
                        "requested_assembly": {"assembly_id": requested["assembly_id"], "ref": requested["ref"],
                                               "link": "confirmed", "unconfirmed_reason": None,
                                               "matched": {"text": found, "source": "path"},
                                               "matched_core": found, "evidence": [cited], "basis": BASIS},
                        "rationale": ("A spreadsheet whose file name names this requested assembly; "
                                      if mapped[file_id].matched_in == "file_name" else
                                      "A spreadsheet whose folder or archive name names this requested assembly; ")
                                     + "mapped by the name-match pre-pass.",
                        "evidence": [cited],
                        "findings": [_finding("ROLE_FROM_NAME_ONLY", "warning",
                                              "The role rests on the file name, folder and type only, not on cell contents.")]})
    linked = sorted(mapped, key=lambda value: int(value[1:]))
    others = [file_id for file_id in view.ids if file_id not in mapped]
    findings = [_finding("SURVEY_NAME_MATCH", "information",
                         "Each requested assembly is named by its spreadsheets' file or folder names; those files were "
                         "mapped from their paths and the model survey was not run. Where an assembly has "
                         "several, the package choice step picks its primary BOM.", linked)]
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
    # Provenance (QQ #99): which files were examined (here, from their names alone), which were not, and why.
    survey["examination"] = {"version": PREPASS_VERSION, "stopped_early": bool(others), "basis": BASIS,
                             "files": [{"file_id": file_id, "status": "examined" if file_id in mapped else "not_examined",
                                        "reason": None if file_id in mapped else "not_needed"} for file_id in view.ids]}
    survey["prepass"] = {"version": PREPASS_VERSION, "links": [
        {"file_id": file_id, "assembly_id": mapped[file_id][0]["assembly_id"], "matched_text": mapped[file_id][1],
         "file_name_revision": mapped[file_id][2], "matched_in": mapped[file_id][3]} for file_id in linked]}
    return survey
