"""Compare parsed BOMs by content and choose one primary BOM per assembly.

Inputs: canonical parsed rows (core.CANONICAL_COLUMNS) of BOMs. Output: bounded
alignment evidence and a primary choice, or no choice. Pure, deterministic,
stdlib only; no I/O. Run python -m pytest tests/test_bom_selection.py.

Rows are aligned by data, not by column names or exact text:
1. placed lines by their exact reference-designator set (simple ranges such as
   R1-R4 are expanded);
2. the item-number relation between aligned lines is learned (identical, or one
   side carries a fixed prefix/suffix) and must hold one-to-one on every aligned
   line before it is used;
3. unplaced lines (no reference designators) by transformed item number, then
   by manufacturer part number.
A BOM is redundant only when every one of its lines is found in the primary
with no conflicting quantity or part number. Anything else is never decided here.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Any, Iterable, Mapping

from .semantic import _norm, _number

VERSION = "bom-content-alignment.v1"
_RANGE = re.compile(r"^([a-z]+)(\d+)-([a-z]*)(\d+)$")
_MAX_RANGE = 500
_MAX_EXAMPLES = 10


def _refdes(value: Any) -> frozenset[str]:
    tokens: set[str] = set()
    for token in re.split(r"[,;\s]+", _norm(value)):
        if not token:
            continue
        match = _RANGE.match(token)
        if match and match.group(3) in ("", match.group(1)):
            start, end = int(match.group(2)), int(match.group(4))
            if start <= end and end - start <= _MAX_RANGE:
                tokens.update(f"{match.group(1)}{index}" for index in range(start, end + 1))
                continue
        tokens.add(token)
    return frozenset(tokens)


def _mpns(row: Mapping[str, Any]) -> frozenset[str]:
    return frozenset(value for index in range(1, 9)
                     if (value := _norm(row.get(f"manufacturer_item_number_{index}"))))


class _Line:
    __slots__ = ("index", "item", "refdes", "mpns", "quantity", "category", "filled")

    def __init__(self, index: int, row: Mapping[str, Any]):
        self.index = index
        self.item = _norm(row.get("item_number"))
        self.refdes = _refdes(row.get("reference_designator"))
        self.mpns = _mpns(row)
        self.quantity = _number(row.get("quantity"))
        self.category = _norm(row.get("item_category"))
        self.filled = sum(1 for value in row.values() if _norm(value))


def _lines(rows: Iterable[Mapping[str, Any]]) -> list[_Line]:
    return [_Line(index, row) for index, row in enumerate(rows)]


def _transform(pairs: list[tuple[str, str]]) -> dict[str, Any] | None:
    """Learn one item-number relation that holds one-to-one on every aligned pair."""
    if not pairs or any(not left or not right for left, right in pairs):
        return None
    if len({left for left, _ in pairs}) != len(pairs) or len({right for _, right in pairs}) != len(pairs):
        return None
    if all(left == right for left, right in pairs):
        return {"kind": "identical"}
    for side, longer, shorter in (("left", 0, 1), ("right", 1, 0)):
        for kind in ("prefix", "suffix"):
            affixes = set()
            for pair in pairs:
                long_value, short_value = pair[longer], pair[shorter]
                if len(long_value) <= len(short_value):
                    break
                if kind == "prefix" and long_value.endswith(short_value):
                    affixes.add(long_value[:-len(short_value)])
                elif kind == "suffix" and long_value.startswith(short_value):
                    affixes.add(long_value[len(short_value):])
                else:
                    break
            else:
                if len(affixes) == 1:
                    return {"kind": kind, "carried_by": side, "affix": affixes.pop()}
    return None


def _apply(transform: dict[str, Any] | None, value: str, side: str) -> str | None:
    """Map an item number to the left BOM's form; None when no relation is proven."""
    if transform is None or not value:
        return None
    if transform["kind"] == "identical":
        return value
    affix = transform["affix"]
    if transform["carried_by"] == side:
        return value
    return affix + value if transform["kind"] == "prefix" else value + affix


def _same_line(left: _Line, right: _Line) -> str | None:
    """None when consistent, else the conflicting attribute."""
    if left.quantity and right.quantity and left.quantity != right.quantity:
        return "quantity"
    if left.mpns and right.mpns and not left.mpns & right.mpns:
        return "manufacturer_part_number"
    return None


def align_boms(left_rows: Iterable[Mapping[str, Any]],
               right_rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Return relation identical / superset / subset / overlap / conflict with counts.

    superset: left contains every right line (right is redundant against left);
    subset: the reverse. overlap: neither contains the other, no conflicts.
    """
    left, right = _lines(left_rows), _lines(right_rows)
    matches: list[tuple[_Line, _Line]] = []
    conflicts: list[dict[str, Any]] = []
    split_refdes = 0
    left_placed = {line.refdes: line for line in left if line.refdes}
    used_left: set[int] = set()
    used_right: set[int] = set()
    duplicates = (len(left_placed) != sum(1 for line in left if line.refdes)
                  or len({line.refdes for line in right if line.refdes})
                  != sum(1 for line in right if line.refdes))
    for line in right:
        if not line.refdes:
            continue
        other = left_placed.get(line.refdes)
        if other is None:
            if any(line.refdes & candidate for candidate in left_placed):
                split_refdes += 1  # Same placements grouped differently: never guessed.
            continue
        matches.append((other, line))
        used_left.add(other.index)
        used_right.add(line.index)
    transform = _transform([(a.item, b.item) for a, b in matches])
    # Unplaced lines: item number under the proven relation, else part numbers.
    remaining_left = [line for line in left if line.index not in used_left and not line.refdes]
    by_item: dict[str, list[_Line]] = {}
    by_mpn: dict[frozenset[str], list[_Line]] = {}
    for line in remaining_left:
        by_item.setdefault(line.item, []).append(line)
        if line.mpns:
            by_mpn.setdefault(line.mpns, []).append(line)
    for line in right:
        if line.index in used_right or line.refdes:
            continue
        key = _apply(transform, line.item, "right")
        candidates = [item for item in by_item.get(key or "", []) if item.index not in used_left]
        if not candidates and line.mpns:
            candidates = [item for item in by_mpn.get(line.mpns, []) if item.index not in used_left]
        if len(candidates) >= 1:
            matches.append((candidates[0], line))
            used_left.add(candidates[0].index)
            used_right.add(line.index)
    for a, b in matches:
        attribute = _same_line(a, b)
        if attribute is not None:
            conflicts.append({"left_line": a.index + 1, "right_line": b.index + 1, "attribute": attribute})
    left_only = [line.index + 1 for line in left if line.index not in used_left]
    right_only = [line.index + 1 for line in right if line.index not in used_right]
    left_refs = set().union(*(line.refdes for line in left)) if left else set()
    right_refs = set().union(*(line.refdes for line in right)) if right else set()
    if conflicts or split_refdes or duplicates:
        relation = "conflict"
    elif not left_only and not right_only:
        relation = "identical"
    elif not right_only and right_refs <= left_refs:
        relation = "superset"
    elif not left_only and left_refs <= right_refs:
        relation = "subset"
    else:
        relation = "overlap"
    placed_matches = sum(1 for a, _ in matches if a.refdes)
    return {"version": VERSION, "relation": relation,
            "left_lines": len(left), "right_lines": len(right),
            "matched_lines": len(matches), "matched_placed_lines": placed_matches,
            "left_only_lines": len(left_only), "right_only_lines": len(right_only),
            "left_only_examples": left_only[:_MAX_EXAMPLES], "right_only_examples": right_only[:_MAX_EXAMPLES],
            "left_refdes": len(left_refs), "right_refdes": len(right_refs),
            "left_only_refdes": len(left_refs - right_refs), "right_only_refdes": len(right_refs - left_refs),
            "conflicting_lines": len(conflicts), "conflict_examples": conflicts[:_MAX_EXAMPLES],
            "split_refdes_lines": split_refdes, "duplicate_refdes_sets": duplicates,
            "item_number_relation": transform}


def _completeness(rows: list[Mapping[str, Any]]) -> int:
    return sum(line.filled for line in _lines(rows))


def select_primary(boms: Mapping[str, list[Mapping[str, Any]]]) -> dict[str, Any]:
    """Choose the BOM that contains every other BOM for one assembly.

    boms maps a stable key (fixture sha256) to parsed rows. Returns
    {primary, roles: {key: primary|redundant|identical}, comparisons, reason}.
    primary is None (human review) when no single BOM contains all the others.
    """
    keys = sorted(boms)
    if not keys:
        raise ValueError("at least one BOM is required")
    if len(keys) == 1:
        return {"version": VERSION, "primary": keys[0], "roles": {keys[0]: "primary"},
                "comparisons": [], "reason": "single_bom"}
    comparisons = {}
    for index, left in enumerate(keys):
        for right in keys[index + 1:]:
            comparisons[(left, right)] = align_boms(boms[left], boms[right])

    def relation(a: str, b: str) -> str:
        if (a, b) in comparisons:
            return comparisons[(a, b)]["relation"]
        return {"superset": "subset", "subset": "superset"}.get(comparisons[(b, a)]["relation"],
                                                                comparisons[(b, a)]["relation"])

    containers = [key for key in keys
                  if all(relation(key, other) in ("identical", "superset") for other in keys if other != key)]
    records = [{"left": a, "right": b, **value} for (a, b), value in comparisons.items()]
    if not containers:
        reason = ("conflict" if any(item["relation"] == "conflict" for item in records)
                  else "no_bom_contains_all_others")
        return {"version": VERSION, "primary": None, "roles": {}, "comparisons": records, "reason": reason}
    # Identical containers: the more completely populated one, then a stable key.
    primary = max(containers, key=lambda key: (_completeness(boms[key]), key))
    roles = {key: "primary" if key == primary else
             ("identical" if relation(primary, key) == "identical" else "redundant") for key in keys}
    return {"version": VERSION, "primary": primary, "roles": roles, "comparisons": records,
            "reason": "contains_all_others"}


def attribute_undeclared(undeclared: list[Mapping[str, Any]],
                         declared: Mapping[str, list[Mapping[str, Any]]]) -> dict[str, Any]:
    """Attribute a BOM that names no assembly to the one declared BOM that contains it.

    declared maps an assembly key to its candidate primary rows. Returns
    {assembly: key or None, comparisons}; None unless exactly one assembly's BOM
    contains (identical or superset) every line of the undeclared BOM.
    """
    comparisons = {key: align_boms(rows, undeclared) for key, rows in sorted(declared.items())}
    containing = [key for key, value in comparisons.items() if value["relation"] in ("identical", "superset")]
    return {"version": VERSION, "assembly": containing[0] if len(containing) == 1 else None,
            "reason": ("contained_in_one_assembly" if len(containing) == 1 else
                       "contained_in_several_assemblies" if containing else "not_contained"),
            "comparisons": comparisons}
