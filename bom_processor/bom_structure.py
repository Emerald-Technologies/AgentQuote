"""Sub-assembly structure of one parsed BOM: quantity roll-down and build/buy proposals.

Sub-assembly plan unit 3a (QuickQuote Docs/SUBASSEMBLY_BOM_PLAN_2026-10-12.md, Nick 2026-10-12).
Pure code, no I/O, no model. Runs after the row check has proven each line's parent.

Inputs: the parsed lines in source order (output row, part number, description, category, quantity
as printed, whether any MPN is given), each line's proven parent (output row or None), whether the
structure was proven, and a person's confirmed choices keyed by line key.
Outputs per line: parent, level, quantity per parent, quantity per quoted assembly (approved G8
roll-down), procurement (buy / build / reference / service), and the proposal's reasons; plus findings
(code, severity, line keys) for the review, CQ and final-pricing warnings.

Decisions it follows (Nick 2026-10-12):
- A part used under several sub-assemblies stays one line per occurrence (no merging).
- Build or buy is a judgement from clues, never a fixed rule: a circuit-board assembly is usually
  built; a cable, mechanical item or programmed part is usually bought; programming is an outside
  service with the blank part priced. Weak or conflicting clues need a person ("needs a decision").
- CQ is never blocked; final pricing warns on proposals and blocks only on needs-a-decision.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

VERSION = "bom-structure.v1"
PROCUREMENT = ("buy", "build", "reference", "service")

# Clues read from a sub-assembly's own description and from what it contains. Clues, not rules:
# they only propose; a person confirms. Each is (pattern, reason code).
_BUILD_OWN = ((r"\b(?:PCB\s*A(?:SSY|SSEMBLY)?|PCBA|PWA|CCA|CIRCUIT\s+CARD|BOARD\s+(?:ASSY|ASSEMBLY|STACK))\b",
               "NAME_CIRCUIT_ASSEMBLY"),)
_BUY_OWN = ((r"\b(?:CABLE|HARNESS|WIRE|WIRING)\b", "NAME_CABLE"),
            (r"\b(?:ENCLOSURE|CHASSIS|BRACKET|PANEL|HOUSING|COVER|FRAME|MACHINED|SHEET\s*METAL|WELDMENT)\b",
             "NAME_MECHANICAL"),
            (r"\b(?:BOX|FOAM|PACKAGING|CARTON)\b", "NAME_PACKAGING"))
_SERVICE_OWN = ((r"\b(?:PROGRAMM?ED|PROGRAMMING|FIRMWARE|FLASHED)\b", "NAME_PROGRAMMED"),)
_BARE_BOARD = r"\b(?:PCB|PWB|PRINTED\s+(?:CIRCUIT|WIRING)\s+BOARD|BARE\s+BOARD)\b"
_ASSEMBLY_WORD = r"\b(?:ASSY|ASSEMBLY|PCBA)\b"


@dataclass(frozen=True)
class Line:
    output_row: int
    item_number: str
    item_name: str
    category: str              # "Part" or "Document"
    quantity: Any              # as printed (per parent)
    has_mpn: bool


def _quantity(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() and number >= 0 else None


def _text(value: Decimal | None) -> str | None:
    """A quantity as plain text without trailing zeros ("4.000" x "1.000" -> "4")."""
    return None if value is None else format(value.normalize(), "f")


def _matches(table: Sequence[tuple[str, str]], text: str) -> list[str]:
    return [code for pattern, code in table if re.search(pattern, text, re.IGNORECASE)]


def _is_bare_board(line: Line) -> bool:
    # A document line such as "SCHEMATIC PCB ..." or "PCB ARTWORK" is not a board.
    return line.category == "Part" and bool(re.search(_BARE_BOARD, line.item_name, re.IGNORECASE)) and not re.search(
        _ASSEMBLY_WORD, line.item_name, re.IGNORECASE)


def propose(own: Line, children: Sequence[Line], all_children: Mapping[int, Sequence[Line]]) -> tuple[str | None, list[str]]:
    """(proposal or None for needs-a-decision, reasons) for one sub-assembly line."""
    name = own.item_name
    build = _matches(_BUILD_OWN, name)
    buy = _matches(_BUY_OWN, name)
    service = _matches(_SERVICE_OWN, name)
    if any(_is_bare_board(child) for child in children):
        build.append("CONTAINS_BARE_BOARD")
    if any(re.search(_BUILD_OWN[0][0], child.item_name, re.IGNORECASE) and all_children.get(child.output_row)
           for child in children):
        build.append("CONTAINS_CIRCUIT_ASSEMBLY")
    if service and not build:
        return "service", service
    if build and not buy:
        return "build", build
    if buy and not build:
        return "buy", buy
    return None, sorted(set(build + buy + service)) or ["NO_CLUE"]


def _key(path: Sequence[str]) -> str:
    return "/".join(path)


def resolve(lines: Sequence[Line], parents: Mapping[int, int | None], *, proven: bool,
            assembly: str | None = None, choices: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Roll quantities down and propose procurement; see the module docstring.

    ``assembly`` is the quoted assembly's part number; a root line naming it is the assembly itself
    (its quantity never multiplies its children). Without proven structure every line is top level,
    as today, and a structure claim that could not be proven is reported, never guessed.
    """
    choices = dict(choices or {})
    findings: list[dict[str, Any]] = []
    by_row = {line.output_row: line for line in lines}
    parent = {line.output_row: (parents.get(line.output_row) if proven else None) for line in lines}
    if any(p is not None and p not in by_row for p in parent.values()):
        raise ValueError("STRUCTURE_PARENT_UNKNOWN")
    children: dict[int, list[Line]] = {}
    for line in lines:
        if parent[line.output_row] is not None:
            children.setdefault(parent[line.output_row], []).append(line)
    if not proven and any(parents.get(line.output_row) is not None for line in lines):
        findings.append({"code": "STRUCTURE_NOT_PROVEN", "severity": "blocking", "lines": []})

    def is_assembly_itself(line: Line) -> bool:
        return (parent[line.output_row] is None and assembly is not None
                and line.item_number.strip().casefold() == assembly.strip().casefold())

    out: list[dict[str, Any]] = []
    resolved: dict[int, dict[str, Any]] = {}
    seen_keys: dict[str, int] = {}
    for line in lines:  # source order: a parent always comes before its children
        row = line.output_row
        up = parent[row]
        above = resolved.get(up) if up is not None else None
        path = (above["path"] if above else []) + [line.item_number]
        key = _key(path)
        seen_keys[key] = seen_keys.get(key, 0) + 1
        if seen_keys[key] > 1:  # the same part twice under one parent: keep both, keys stay distinct
            key = f"{key}#{seen_keys[key]}"
        printed = _quantity(line.quantity)
        if printed is None and line.category != "Document":
            findings.append({"code": "QUANTITY_UNREADABLE", "severity": "blocking", "lines": [key]})
        multiplier = above["multiplier"] if above else Decimal(1)
        itself = is_assembly_itself(line)
        own_multiplier = Decimal(1) if itself else printed
        rolled = Decimal(1) if itself else (None if printed is None or multiplier is None else printed * multiplier)
        record = {"output_row": row, "line_key": key, "parent_line": up,
                  "bom_level": (above["bom_level"] + 1) if above else (0 if is_assembly_itself(line) else 1),
                  "quantity_per_parent": _text(printed), "quantity": _text(rolled),
                  "path": path, "multiplier": None if multiplier is None or own_multiplier is None
                  else multiplier * own_multiplier}
        ancestor = above["procurement"] if above else None
        if line.category == "Document":
            procurement, basis = "reference", {"kind": "document"}
        elif is_assembly_itself(line):
            procurement, basis = "build", {"kind": "quoted_assembly"}
        elif ancestor in ("buy", "reference") and above and above["is_subassembly"]:
            procurement, basis = "reference", {"kind": "inside_bought_subassembly", "parent": above["line_key"]}
        elif row in children:
            proposal, reasons = propose(line, children[row], children)
            chosen = choices.get(record["line_key"])
            if chosen is not None:
                if chosen not in PROCUREMENT:
                    raise ValueError("CHOICE_INVALID")
                procurement, basis = chosen, {"kind": "confirmed", "proposal": proposal, "reasons": reasons}
            elif proposal is None:
                procurement, basis = None, {"kind": "needs_decision", "reasons": reasons}
                findings.append({"code": "SUBASSEMBLY_NEEDS_DECISION", "severity": "blocking",
                                 "lines": [record["line_key"]]})
            else:
                procurement, basis = proposal, {"kind": "proposed", "reasons": reasons}
                findings.append({"code": "SUBASSEMBLY_PROPOSED", "severity": "warning",
                                 "lines": [record["line_key"]]})
            if procurement == "service":
                findings.append({"code": "PROGRAMMING_CHARGE_TO_ADD", "severity": "warning",
                                 "lines": [record["line_key"]]})
        else:
            procurement, basis = "buy", {"kind": "part"}
        record.update(procurement=procurement, procurement_basis=basis,
                      is_subassembly=row in children and not is_assembly_itself(line))
        resolved[row] = record
        out.append(record)
    # Children of an undecided sub-assembly are priced as parts until a person decides; the
    # finding above already blocks final pricing.
    decided = {record["line_key"] for record in out if record["is_subassembly"]}
    if set(choices) - decided:
        raise ValueError("CHOICE_UNKNOWN_LINE")  # a choice for no sub-assembly is never ignored
    lines_out = [{key: value for key, value in record.items() if key not in ("path", "multiplier")}
                 for record in out]
    return {"version": VERSION, "proven": proven, "lines": lines_out, "findings": findings,
            "summary": {"lines": len(lines_out),
                        "subassemblies": sum(1 for r in lines_out if r["is_subassembly"]),
                        "depth": max((r["bom_level"] for r in lines_out), default=0),
                        "needs_decision": sum(1 for f in findings if f["code"] == "SUBASSEMBLY_NEEDS_DECISION")}}
