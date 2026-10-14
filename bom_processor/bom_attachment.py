"""Separate sub-assembly BOM files: which parsed BOM goes under which line (sub-assembly plan unit 6).

QuickQuote Docs/SUBASSEMBLY_BOM_PLAN_2026-10-12.md section 13 (Nick 2026-10-13: option A plus C; a file
named by several assemblies is used for each; an unclean match is a warning and is not attached).
Pure code, no I/O, no model.

Inputs: one node per parsed BOM group (its assembly identity, its lines' part numbers and revisions in
published order, whether it is a requested assembly) and a callback telling whether a line already has
children in its own file. Outputs: for each parent, the lines whose files are attached (their rows go
under the line) or compared (the parent already lists the parts), the groups that become part of a
parent, the groups set aside because the match is unclear, and findings for the review.

A line names a file when its part number and the file's assembly name each name the other under the
core-number rule (assembly_core), so 010-0810-U2 never names 010-0810-U2-03 or ASM-1-SUB. Packages with
no such line are unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Sequence

from .assembly_core import find_assembly, revision_contradicts

MAX_DEPTH = 10


@dataclass(frozen=True)
class Node:
    key: int
    name: str
    revision: str
    lines: tuple[tuple[str, str], ...]   # (part number, revision) per published line
    requested: bool = False
    attachable: bool = True              # False: never placed under a line (e.g. an unresolved conflict)


def names(part_number: str, assembly: str) -> bool:
    """A line's part number and a file's assembly name name each other (core-number rule)."""
    part_number, assembly = part_number.strip(), assembly.strip()
    if not part_number or not assembly:
        return False
    return find_assembly(assembly, part_number) is not None and find_assembly(part_number, assembly) is not None


def own_line(node: Node) -> int | None:
    """Position of the line that is the file's own assembly (its Level 0 row), if it lists one."""
    return next((index for index, (number, _) in enumerate(node.lines) if names(number, node.name)), None)


def plan(nodes: Sequence[Node], has_children: Callable[[int, int], bool]) -> dict[str, Any]:
    """Attach, compare or set aside. has_children(node key, position) reads the parent's own structure."""
    by_key = {node.key: node for node in nodes}
    edges: dict[tuple[int, int], list[int]] = {}   # (parent, position) -> child keys
    for parent in nodes:
        skip = own_line(parent)
        for position, (number, _) in enumerate(parent.lines):
            if position == skip:
                continue
            for child in nodes:
                if child.key != parent.key and not child.requested and names(number, child.name):
                    edges.setdefault((parent.key, position), []).append(child.key)
    findings: list[dict[str, Any]] = []
    clean: dict[int, list[tuple[int, int, str]]] = {}   # parent -> (position, child, action)
    named: set[int] = set()
    for (parent, position), children in sorted(edges.items()):
        named.update(children)
        if len(children) > 1:
            findings += [{"code": "SUBASSEMBLY_FILE_AMBIGUOUS", "severity": "warning", "parent": parent,
                          "position": position, "child": child} for child in children]
            continue
        child = children[0]
        line_revision = by_key[parent].lines[position][1]
        if revision_contradicts(by_key[child].revision, line_revision):
            findings.append({"code": "SUBASSEMBLY_FILE_REVISION_DIFFERS", "severity": "warning",
                             "parent": parent, "position": position, "child": child})
            continue
        if not by_key[child].attachable:
            findings.append({"code": "SUBASSEMBLY_FILE_NOT_RESOLVED", "severity": "warning",
                             "parent": parent, "position": position, "child": child})
            continue
        action = "compared" if has_children(parent, position) else "attached"
        clean.setdefault(parent, []).append((position, child, action))
    if not edges:
        return {"attach": {}, "compare": {}, "folded": [], "set_aside": [], "findings": []}
    requested = any(node.requested for node in nodes)
    tops = [node.key for node in nodes if (node.requested if requested else node.key not in named)]
    # Breadth first from the quoted assemblies; a file is used under every line that names it.
    reached, depth, frontier = set(tops), {key: 0 for key in tops}, list(tops)
    attach: dict[int, list[tuple[int, int]]] = {}
    compare: dict[int, list[tuple[int, int]]] = {}
    while frontier:
        parent = frontier.pop(0)
        for position, child, action in clean.get(parent, []):
            if child in tops or depth[parent] + 1 > MAX_DEPTH or child in _ancestors(parent, attach, compare):
                findings.append({"code": "SUBASSEMBLY_FILE_CYCLE", "severity": "warning",
                                 "parent": parent, "position": position, "child": child})
                continue
            (attach if action == "attached" else compare).setdefault(parent, []).append((position, child))
            if child not in reached:
                reached.add(child)
                depth[child] = depth[parent] + 1
                frontier.append(child)
    folded = sorted(reached - set(tops))
    uses: dict[int, int] = {}
    for table in (attach, compare):
        for items in table.values():
            for _, child in items:
                uses[child] = uses.get(child, 0) + 1
    for parent, items in attach.items():
        for position, child in items:
            findings.append({"code": "SUBASSEMBLY_FILE_ATTACHED", "severity": "information",
                             "parent": parent, "position": position, "child": child})
    findings += [{"code": "SUBASSEMBLY_FILE_SHARED", "severity": "information", "parent": None,
                  "position": None, "child": child} for child, count in sorted(uses.items()) if count > 1]
    set_aside = sorted(key for key in named - reached if not by_key[key].requested)
    return {"attach": attach, "compare": compare, "folded": folded, "set_aside": set_aside,
            "findings": findings}


def _ancestors(key: int, *tables: dict[int, list[tuple[int, int]]]) -> set[int]:
    parents: dict[int, set[int]] = {}
    for table in tables:
        for parent, items in table.items():
            for _, child in items:
                parents.setdefault(child, set()).add(parent)
    seen, stack = set(), [key]
    while stack:
        for parent in parents.get(stack.pop(), ()):
            if parent not in seen:
                seen.add(parent)
                stack.append(parent)
    return seen | {key}


def _quantity(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    return number.normalize() if number.is_finite() else None


def compare(listed: Sequence[tuple[str, Any]], file_lines: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    """Counts only: the parent's lines under the naming line vs the separate file's lines.

    Each side is (part number, quantity as printed). Parts are matched by part number, ignoring case.
    """
    def table(items: Sequence[tuple[str, Any]]) -> dict[str, list[Decimal | None]]:
        result: dict[str, list[Decimal | None]] = {}
        for number, quantity in items:
            if number.strip():
                result.setdefault(number.strip().casefold(), []).append(_quantity(quantity))
        return result
    left, right = table(listed), table(file_lines)
    both = set(left) & set(right)
    differs = sum(1 for key in both if sorted(map(str, left[key])) != sorted(map(str, right[key])))
    counts = {"in_both": len(both), "only_in_file": len(set(right) - both),
              "only_in_parent": len(set(left) - both), "quantity_differs": differs}
    outcome = "matches" if not (counts["only_in_file"] or counts["only_in_parent"] or differs) else "differs"
    return {"outcome": outcome, **counts}
