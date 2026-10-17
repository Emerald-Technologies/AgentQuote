"""Deterministic package-name rules (Nick, 2026-10-14 and D7 2026-10-17, QuickQuote #99).

General rule (D7): a folder, archive or file name on a file's path that names a requested
assembly under the core-number rule (assembly_core.find_assembly: "<PN>", "<PN> Rev B",
"<PN>_RevB", "ASSY <PN>", any common separator) is a clue that the file belongs to it, at any
depth. It is a clue for the model and the only basis on which a file without text may be linked
(decision 6); it never links a file by code and never replaces the survey's own reading. Names
of two assemblies on one path, or a written revision that contradicts the requested one, give
no clue: the file stays shared and a person decides. A file copied into two assemblies' folders
is two files, each with its own clue, so it is used for each.

Board rule (#99 blocker 7): a folder, archive or file name that names a part number which is a
line on exactly one requested assembly's BOM (a bare board such as a 0110- or 0120- number, by the
same core-number rule) is a clue for that assembly and those lines. A part on several assemblies'
BOMs gives no clue, so a person decides. A segment that names a requested assembly itself is read
by the path rule only (a sub-assembly is also a line on its parent's BOM). Clue only, like the
path rule; short part numbers (under six letters and digits, or no digit) are never used.

Approved rules for one package family, deliberately narrow (name and core-number clues only);
they take precedence over the general rule on the segments they match:
- a folder or archive named AMT0100-<core>-TK belongs to the requested assembly 0100-<core>;
- a folder or archive named AMT0110-<core> is the bare board 0110-<core>; it belongs to the one
  assembly whose approved BOM lists 0110-<core> as a Part line, and to those lines;
- a root file named BOM 0100-<core> REV <r>.xlsx belongs to assembly 0100-<core> at revision <r>
  (a placement clue only; original BOM inputs stay retained provenance).

Inputs: inventory items with paths, requested assemblies and verified BOM occurrences.
Output: evidenceId -> rule match. Any conflict (two rules naming different assemblies, a board on
several assemblies' BOMs, no or several matching assemblies, a revision that differs) gives no
match, so the file stays with the model and a person. Packages that do not use these names are
unaffected by those three rules. Run python -m pytest tests/test_package_evidence_name_rules.py.
"""
from __future__ import annotations

import re

from .assembly_core import find_assembly, revision_contradicts
from .package_survey_check import cited_revision

# v2 (D7, 2026-10-17): the general path rule.
RULE_VERSION = "package-name-rules.v2"
PATH_RULE = "path"
BOM_LINE_RULE = "bom_line"
# Rules that only give the model and a person a clue; kit and board rules link by code.
CLUE_RULES = frozenset({PATH_RULE, BOM_LINE_RULE})
_KIT = re.compile(r"AMT0100[-_ ]?([0-9]{5})[-_ ]TK", re.I)
_BOARD = re.compile(r"AMT0110[-_ ]?([0-9]{5})", re.I)
_BOM_FILE = re.compile(r"BOM[-_ ]0100[-_ ]([0-9]{5})[-_ ]REV[-_ ]([A-Z0-9]{1,8})\.xlsx", re.I)
_GERBER = {"gbr", "ger", "gerber", "art", "pho", "gtl", "gbl", "gts", "gbs", "gto", "gbo", "gtp", "gbp", "gko", "gm1"}
_DRILL = {"drl", "xln", "exc", "nc", "drd", "tap"}


def _key(value: str) -> str:
    return "".join(re.findall(r"[A-Z0-9]+", str(value).upper()))


def _revision(value: str | None) -> str | None:
    return None if value is None else (_key(value).lstrip("0") or "0")


def _segment_name(segment: str) -> str:
    return segment[:-4] if segment.lower().endswith(".zip") else segment


def _path_owner(segment: str, assemblies: list[dict]) -> tuple[bool, str | None]:
    """(named, owner): whether a path segment names a requested assembly, and which one when exactly one."""
    named = [assembly for assembly in assemblies if find_assembly(assembly["partNumber"], segment) is not None]
    if len(named) != 1:
        return bool(named), None
    assembly = named[0]
    revision = cited_revision(assembly["partNumber"], segment)
    if revision_contradicts(assembly.get("revision"), revision):
        return True, None
    return True, assembly["assemblyUuid"]


def _line_owner(segment: str, lines: dict[str, tuple[str, dict[str, list[str]]]]
                ) -> tuple[bool, str | None, list[str]]:
    """(named, owner, occurrence ids): BOM-line part numbers a path segment names, when on one assembly only."""
    segment_key = _key(segment)
    owners: dict[str, set[str]] = {}
    for key, (part_number, by_assembly) in lines.items():
        if key in segment_key and find_assembly(part_number, segment) is not None:
            for assembly, occurrence_ids in by_assembly.items():
                owners.setdefault(assembly, set()).update(occurrence_ids)
    if len(owners) != 1:
        return bool(owners), None, []
    owner, occurrence_ids = next(iter(owners.items()))
    return True, owner, sorted(occurrence_ids)


def _segment_clue(segment: str, assemblies: list[dict], lines: dict[str, tuple[str, dict[str, list[str]]]]
                  ) -> tuple[str, str | None, list[str]] | None:
    """(rule, owner or None for a conflict, occurrence ids) for one path segment, or None when it names nothing."""
    named, owner = _path_owner(segment, assemblies)
    if named:
        return PATH_RULE, owner, []
    named, owner, occurrence_ids = _line_owner(segment, lines)
    return (BOM_LINE_RULE, owner, occurrence_ids) if named else None


def match_names(items: list[dict], paths: dict[str, str], assemblies: list[dict],
                occurrences: list[dict]) -> dict[str, dict]:
    """evidenceId -> {assembly, occurrenceIds, rule, name, role} for files a rule places.

    rule is kit, board or bom_file (the package-family rules), or path or bom_line (clues).
    """
    by_number: dict[str, list[dict]] = {}
    for assembly in assemblies:
        by_number.setdefault(_key(assembly["partNumber"]), []).append(assembly)
    boards: dict[str, dict[str, list[str]]] = {}
    for occurrence in occurrences:
        boards.setdefault(_key(occurrence["partNumber"]), {}).setdefault(
            occurrence["assemblyUuid"], []).append(occurrence["occurrenceId"])
    lines: dict[str, tuple[str, dict[str, list[str]]]] = {}
    for occurrence in occurrences:
        part_number = occurrence.get("partNumber")
        key = _key(part_number) if isinstance(part_number, str) else ""
        if len(key) >= 6 and any(ch.isdigit() for ch in key):
            lines.setdefault(key, (part_number, {}))[1].setdefault(
                occurrence["assemblyUuid"], []).append(occurrence["occurrenceId"])
    found: dict[str, dict] = {}
    clues_by_text: dict[str, tuple | None] = {}  # Folder names repeat on many paths.
    for item in items:
        identity = item["evidenceId"]
        segments = paths[identity].split("/")
        targets = []
        for index, segment in enumerate(segments):
            name = _segment_name(segment)
            kit = _KIT.fullmatch(name)
            board = None if kit else _BOARD.fullmatch(name)
            bom = _BOM_FILE.fullmatch(segment) if index == 0 and len(segments) == 1 else None
            if kit:
                matched = by_number.get("0100" + kit.group(1), [])
                targets.append(("kit", name, matched[0]["assemblyUuid"] if len(matched) == 1 else None, []))
            elif board:
                owners = boards.get("0110" + board.group(1), {})
                owner = next(iter(owners)) if len(owners) == 1 else None
                targets.append(("board", name, owner, sorted(owners.get(owner, [])) if owner else []))
            elif bom:
                matched = by_number.get("0100" + bom.group(1), [])
                same = len(matched) == 1 and _revision(matched[0]["revision"]) == _revision(bom.group(2))
                targets.append(("bom_file", segment, matched[0]["assemblyUuid"] if same else None, []))
            else:
                text = segment if index == len(segments) - 1 else name
                if text not in clues_by_text:
                    clues_by_text[text] = _segment_clue(text, assemblies, lines)
                clue = clues_by_text[text]
                if clue is not None:
                    targets.append((clue[0], text, clue[1], clue[2]))
        if not targets or any(owner is None for _, _, owner, _ in targets):
            continue
        if len({owner for _, _, owner, _ in targets}) != 1:
            continue   # Rules name different assemblies: a person decides.
        boards_named = [t for t in targets if t[0] == "board"]
        if len({t[1].lower() for t in boards_named}) > 1:
            continue
        # A package-family rule outranks the clues on the same path; a BOM-line clue outranks a path clue.
        family = [t for t in targets if t[0] not in CLUE_RULES]
        board_lines = [t for t in targets if t[0] == BOM_LINE_RULE]
        rule, name, owner, occurrence_ids = (boards_named[0] if boards_named
                                             else (family or board_lines or targets)[-1])
        found[identity] = {"assembly": owner, "occurrenceIds": occurrence_ids, "rule": rule, "name": name,
                           "role": role_for(rule, item, paths[identity])}
    return found


def role_for(rule: str, item: dict, path: str) -> str:
    """Code-owned role from file type: Gerber artwork, drill data or drawing. Confirmed by a person."""
    suffix = path.rsplit("/", 1)[-1].rsplit(".", 1)[-1].lower() if "." in path.rsplit("/", 1)[-1] else ""
    if rule == "board":
        if item.get("format") == "gerber_text" or suffix in _GERBER:
            return "artwork"
        if item.get("format") == "excellon_text" or suffix in _DRILL:
            return "drill"
        return "fabrication_drawing"
    return "assembly_drawing"


def rationale(match: dict, assembly_name: str) -> str:
    if match["rule"] == "board":
        return (f"Package name rule: {match['name']} is the bare board on the BOM of {assembly_name}. "
                "Linked from the folder or archive name, not from file text; a person confirms.")
    if match["rule"] == "kit":
        return (f"Package name rule: {match['name']} belongs to assembly {assembly_name}. "
                "Linked from the folder or archive name, not from file text; a person confirms.")
    if match["rule"] == BOM_LINE_RULE:
        return (f"Board clue: {match['name']} names a part on the BOM of {assembly_name} only. "
                "From the file, folder or archive name, not from file text; a person confirms.")
    if match["rule"] == PATH_RULE:
        return (f"Path clue: {match['name']} names assembly {assembly_name}. "
                "From the file, folder or archive name, not from file text; a person confirms.")
    return f"Package name rule: {match['name']} is the BOM file of {assembly_name}."
