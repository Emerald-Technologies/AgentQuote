"""Rank package files by how likely each is a BOM, before any model call (QQ #99, Nick 2026-10-14).

"Treat CSVs as far less likely candidates to be BOMs and spreadsheets at higher levels in the
package as far more likely ... look at the highest likelihood candidates first ... I personally
would never waste my time going that many levels deep to find some CSV file." A person looks at
the shallow, BOM-named workbooks first and stops once each requested assembly has its BOM.

Clues, not rules: each clue adds or takes away a little, and only the total decides the order.
- depth, relative to the shallowest spreadsheet in the package (shallow >> deep);
- type (xlsx/xls/xlsm >> csv; a non-spreadsheet is never a BOM candidate here);
- name: BOM, bill of materials, parts list (strong), PL (weak);
- context: Gerber, pick and place, centroid, ICT, test point, fab, drill, ODB and the like argue
  against; a PCB folder argues slightly against;
- the file or folder name, or a shown top-row cell, names a requested assembly (core-number rule);
- a shown row reads like BOM headings (a quantity column plus a part, designator or description).
A spreadsheet scoring at least LIKELY_SCORE is a likely candidate: it is always surveyed. Less likely
files are surveyed only while a requested assembly still lacks a confirmed BOM.

refusal_blocks decides, from evidence, whether a file whose survey reading was refused after its
retries makes the BOM choice impossible or unsafe (fail closed), or can be listed as not examined.
Pure; no I/O and no model calls. Run python -m pytest tests/test_package_survey_rank.py.
"""
from __future__ import annotations

import re
from typing import Any

from .assembly_core import find_assembly
from .package_survey import SPREADSHEET_KINDS, SurveyView, cell_text

RANK_VERSION = "package-survey-rank.v1"
LIKELY_SCORE = 2
_KIND_SCORE = {"spreadsheet_ooxml": 2, "spreadsheet_xls": 2, "spreadsheet_html_xls": 2, "spreadsheet_csv": -1}
# Relative depth 0, 1, 2; deeper scores _DEEP.
_DEPTH_SCORE = (3, 1, 0)
_DEEP = -2
_NOT_A_SPREADSHEET = -10


def _words(*alternatives: str) -> re.Pattern[str]:
    """Whole words (letters and digits on neither side), spaces matching any separator."""
    body = "|".join(part.replace(" ", r"[\s_.\-]*") for part in alternatives)
    return re.compile(r"(?i)(?<![a-z0-9])(?:" + body + r")(?![a-z0-9])")


_BOM_NAME = _words("boms?", "bill of materials?", "parts? lists?", "mod bom")
_PL_NAME = _words("pl")
_AGAINST = _words("gerbers?", "pick and place", "pick n place", "pnp", "p&p", "centroids?", "xy", "placement",
                  "ict", "test ?points?", "fab", "fabrication", "drill", "odb", r"odb\+\+", "cad", "panel",
                  "stencil", "paste", "silk ?screen", "netlist", "ipc ?356", "ipc ?d ?356")
_PCB = _words("pcb", "pcbs")
_QUANTITY = _words("qty", "quantity", "qnty", "qty per", "qty/assy")
_OTHER_HEADINGS = (_words("ref ?des", "reference ?designators?", "designators?", "refs?", "reference", "location"),
                   _words("part ?number", "part ?no", "part ?#", "p/?n", "mpn", "mfr", "mfg", "manufacturer",
                          "manufacturer part", "vendor part", "item number"),
                   _words("description", "desc", "value"))


def _depth(path: str) -> int:
    return path.count("/")


def bom_headings(view: SurveyView, file_id: str) -> bool:
    """Whether a shown row reads like BOM column headings: a quantity plus another BOM heading."""
    rows: dict[tuple[str, int], list[str]] = {}
    for (sheet, row, _), value in view.shown.get(file_id, {}).items():
        rows.setdefault((sheet, row), []).append(cell_text(value))
    for cells in rows.values():
        if any(_QUANTITY.search(text) and len(text) <= 40 for text in cells):
            if any(pattern.search(text) and len(text) <= 60 for pattern in _OTHER_HEADINGS for text in cells):
                return True
    return False


def named_assemblies(view: SurveyView, file_id: str) -> dict[str, list[str]]:
    """Requested assembly ids named (core-number rule) by the file name, its folders, or its shown cells."""
    path = view.files[file_id].path
    folder, _, name = path.rpartition("/")
    cells = [cell_text(value) for value in view.shown.get(file_id, {}).values()]
    found: dict[str, list[str]] = {"name": [], "folder": [], "cells": []}
    for item in view.requested:
        if find_assembly(item["part_number"], name) is not None:
            found["name"].append(item["assembly_id"])
        if folder and find_assembly(item["part_number"], folder) is not None:
            found["folder"].append(item["assembly_id"])
        if any(find_assembly(item["part_number"], text) is not None for text in cells):
            found["cells"].append(item["assembly_id"])
    return found


def rank_files(view: SurveyView) -> list[dict[str, Any]]:
    """Every file, most likely BOM first: {file_id, rank, score, likely, clues}. Deterministic."""
    sheets = [file_id for file_id in view.ids if view.entries[file_id]["kind"] in SPREADSHEET_KINDS]
    base = min((_depth(view.files[file_id].path) for file_id in sheets or view.ids), default=0)
    scored = []
    for file_id in view.ids:
        path, kind = view.files[file_id].path, view.entries[file_id]["kind"]
        relative = max(_depth(path) - base, 0)
        clues = [f"depth_{relative}"]
        score = _DEPTH_SCORE[relative] if relative < len(_DEPTH_SCORE) else _DEEP
        if kind in SPREADSHEET_KINDS:
            score += _KIND_SCORE[kind]
            clues.append("csv" if kind == "spreadsheet_csv" else "workbook")
        else:
            score += _NOT_A_SPREADSHEET
            clues.append("not_a_spreadsheet")
        name = path.rpartition("/")[2]
        if _BOM_NAME.search(name):
            score += 3
            clues.append("bom_name")
        elif _PL_NAME.search(name):
            score += 1
            clues.append("pl_name")
        if _AGAINST.search(path):
            score -= 3
            clues.append("non_bom_context")
        elif _PCB.search(path):
            score -= 1
            clues.append("pcb_context")
        named = named_assemblies(view, file_id) if view.requested else {"name": [], "folder": [], "cells": []}
        if named["name"]:
            score += 2
            clues.append("names_requested_in_name")
        elif named["folder"]:
            score += 1
            clues.append("names_requested_in_folder")
        if named["cells"]:
            score += 1
            clues.append("names_requested_in_cells")
        if kind in SPREADSHEET_KINDS and bom_headings(view, file_id):
            score += 2
            clues.append("bom_headings")
        scored.append({"file_id": file_id, "score": score,
                       "likely": kind in SPREADSHEET_KINDS and score >= LIKELY_SCORE, "clues": clues,
                       "_order": (-score, _depth(path), path.casefold(), path)})
    scored.sort(key=lambda item: item.pop("_order"))
    return [{"rank": index, **item} for index, item in enumerate(scored, 1)]


def confirmed_assemblies(records: list[dict[str, Any]]) -> set[str]:
    """Requested assembly ids with at least one examined BOM whose link code confirmed."""
    return {record["requested_assembly"]["assembly_id"] for record in records
            if record.get("role") == "bom" and (record.get("requested_assembly") or {}).get("link") == "confirmed"}


def every_assembly_confirmed(view: SurveyView, records: list[dict[str, Any]]) -> bool:
    return bool(view.requested) and {item["assembly_id"] for item in view.requested} <= confirmed_assemblies(records)


def refusal_blocks(view: SurveyView, ranking: list[dict[str, Any]], records: list[dict[str, Any]],
                   refused: list[str], proposed: dict[str, str]) -> dict[str, str | None]:
    """file id -> why its refused reading blocks the package, or None when it can be not examined.

    records are the files whose readings passed; proposed is the role the model gave a refused file
    (unverified, a clue only). Fail closed for real BOM candidates:
    - all_bom_candidates_failed: no examined file is a BOM;
    - no_confirmed_bom_link: no examined BOM has a confirmed link to a requested assembly;
    - assembly_without_bom: a requested assembly has no confirmed BOM, and a refused spreadsheet
      could be it;
    - possible_bom_candidate: a likely candidate whose name or cells name a requested assembly,
      whose name or headings read like a BOM, or that the model itself called a BOM.
    A refused non-spreadsheet never blocks (BOMs are read from spreadsheets). A less likely file,
    or a likely one with no BOM clue, is listed as not examined while each requested assembly has
    a confirmed BOM.
    """
    by_id = {item["file_id"]: item for item in ranking}
    examined_hashes = {record["sha256"] for record in records}
    sheets = [file_id for file_id in refused if view.entries[file_id]["kind"] in SPREADSHEET_KINDS]
    overall = None
    if sheets:
        if not any(record.get("role") == "bom" for record in records):
            overall = "all_bom_candidates_failed"
        elif not confirmed_assemblies(records):
            overall = "no_confirmed_bom_link"
        elif not every_assembly_confirmed(view, records):
            overall = "assembly_without_bom"
    blocks: dict[str, str | None] = {}
    for file_id in refused:
        if file_id not in sheets:
            blocks[file_id] = None
        elif overall is not None:
            blocks[file_id] = overall
        elif view.files[file_id].sha256 in examined_hashes:
            # Identical bytes were examined: this copy holds nothing the choice has not seen.
            blocks[file_id] = None
        else:
            item = by_id[file_id]
            clue = (proposed.get(file_id) == "bom" or bool({"bom_name", "bom_headings", "names_requested_in_name",
                                                             "names_requested_in_folder", "names_requested_in_cells"}
                                                            & set(item["clues"])))
            blocks[file_id] = "possible_bom_candidate" if item["likely"] and clue else None
    return blocks
