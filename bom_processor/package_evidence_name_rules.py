"""Deterministic package-name rules for one package family (Nick, 2026-10-14, QuickQuote #99).

Approved rules, deliberately narrow (name and core-number clues only):
- a folder or archive named AMT0100-<core>-TK belongs to the requested assembly 0100-<core>;
- a folder or archive named AMT0110-<core> is the bare board 0110-<core>; it belongs to the one
  assembly whose approved BOM lists 0110-<core> as a Part line, and to those lines;
- a root file named BOM 0100-<core> REV <r>.xlsx belongs to assembly 0100-<core> at revision <r>
  (a placement clue only; original BOM inputs stay retained provenance).

Inputs: inventory items with paths, requested assemblies and verified BOM occurrences.
Output: evidenceId -> rule match. Any conflict (two rules naming different assemblies, a board on
several assemblies' BOMs, no or several matching assemblies, a revision that differs) gives no
match, so the file stays with the model and a person. Packages that do not use these names are
unaffected. Run python -m pytest tests/test_package_evidence_name_rules.py.
"""
from __future__ import annotations

import re

RULE_VERSION = "package-name-rules.v1"
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


def match_names(items: list[dict], paths: dict[str, str], assemblies: list[dict],
                occurrences: list[dict]) -> dict[str, dict]:
    """evidenceId -> {assembly, occurrenceIds, rule, name, role} for files a rule places."""
    by_number: dict[str, list[dict]] = {}
    for assembly in assemblies:
        by_number.setdefault(_key(assembly["partNumber"]), []).append(assembly)
    boards: dict[str, dict[str, list[str]]] = {}
    for occurrence in occurrences:
        boards.setdefault(_key(occurrence["partNumber"]), {}).setdefault(
            occurrence["assemblyUuid"], []).append(occurrence["occurrenceId"])
    found: dict[str, dict] = {}
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
        if not targets or any(owner is None for _, _, owner, _ in targets):
            continue
        if len({owner for _, _, owner, _ in targets}) != 1:
            continue   # Rules name different assemblies: a person decides.
        boards_named = [t for t in targets if t[0] == "board"]
        if len({t[1].lower() for t in boards_named}) > 1:
            continue
        rule, name, owner, lines = boards_named[0] if boards_named else targets[-1]
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
    return f"Package name rule: {match['name']} is the BOM file of {assembly_name}."
