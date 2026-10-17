"""Deterministic package-name rules (Nick, 2026-10-14 and D7 2026-10-17, QuickQuote #99).

General rule (D7): a folder, archive or file name on a file's path that names a requested
assembly under the core-number rule (assembly_core.find_assembly: "<PN>", "<PN> Rev B",
"<PN>_RevB", "ASSY <PN>", any common separator) is a clue that the file belongs to it, at any
depth. It is a clue for the model and the only basis on which a file without text may be linked
(decision 6); it never links a file by code and never replaces the survey's own reading. Names
of two assemblies on one path, or a written revision that contradicts the requested one, give
no clue: the file stays shared and a person decides. A file copied into two assemblies' folders
is two files, each with its own clue, so it is used for each.

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


def match_names(items: list[dict], paths: dict[str, str], assemblies: list[dict],
                occurrences: list[dict]) -> dict[str, dict]:
    """evidenceId -> {assembly, occurrenceIds, rule, name, role} for files a rule places.

    rule is kit, board or bom_file (the package-family rules) or path (the general clue).
    """
    by_number: dict[str, list[dict]] = {}
    for assembly in assemblies:
        by_number.setdefault(_key(assembly["partNumber"]), []).append(assembly)
    boards: dict[str, dict[str, list[str]]] = {}
    for occurrence in occurrences:
        boards.setdefault(_key(occurrence["partNumber"]), {}).setdefault(
            occurrence["assemblyUuid"], []).append(occurrence["occurrenceId"])
    found: dict[str, dict] = {}
    owners_by_text: dict[str, tuple[bool, str | None]] = {}  # Folder names repeat on many paths.
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
                if text not in owners_by_text:
                    owners_by_text[text] = _path_owner(text, assemblies)
                named, owner = owners_by_text[text]
                if named:
                    targets.append((PATH_RULE, text, owner, []))
        if not targets or any(owner is None for _, _, owner, _ in targets):
            continue
        if len({owner for _, _, owner, _ in targets}) != 1:
            continue   # Rules name different assemblies: a person decides.
        boards_named = [t for t in targets if t[0] == "board"]
        if len({t[1].lower() for t in boards_named}) > 1:
            continue
        # A package-family rule outranks the general path clue on the same path.
        family = [t for t in targets if t[0] != PATH_RULE]
        rule, name, owner, lines = boards_named[0] if boards_named else (family or targets)[-1]
        found[identity] = {"assembly": owner, "occurrenceIds": lines, "rule": rule, "name": name,
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
    if match["rule"] == PATH_RULE:
        return (f"Path clue: {match['name']} names assembly {assembly_name}. "
                "From the file, folder or archive name, not from file text; a person confirms.")
    return f"Package name rule: {match['name']} is the BOM file of {assembly_name}."
