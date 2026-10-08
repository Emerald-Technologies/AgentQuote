"""Compare parsed BOMs by content and choose one primary BOM per assembly.

Inputs: canonical parsed rows (core.CANONICAL_COLUMNS) of BOMs. Output: bounded
alignment evidence and a primary choice, or no choice. Pure, deterministic,
stdlib only; no I/O. Run python -m pytest tests/test_bom_selection.py.

Lines are matched by part identity, the way a person reads a BOM:
1. manufacturer part number: lines sharing one, unique within each BOM;
2. item number, after learning the relation between the two BOMs' item
   numbers from the lines matched in step 1 (identical, or one side carries a
   fixed prefix/suffix). The relation must hold one-to-one on every such line
   before it is used.
Reference designators are optional and not relied on: they may be missing from
one or both BOMs. They never match lines and never cause a conflict; when both
lines carry them, agreement is only counted as supporting evidence.
A BOM is redundant only when every one of its lines is found in the primary
with no conflicting quantity or part number. Anything else is never decided here.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Any, Iterable, Mapping

from .semantic import _norm, _number

VERSION = "bom-content-alignment.v2"
_RANGE = re.compile(r"^([a-z]+)(\d+)-([a-z]*)(\d+)$")
_MAX_RANGE = 500
_MAX_EXAMPLES = 10


def _refdes(value: Any) -> frozenset[str]:
    """Supporting evidence only; simple ranges such as R1-R4 are expanded."""
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
    __slots__ = ("index", "item", "refdes", "mpns", "quantity", "filled")

    def __init__(self, index: int, row: Mapping[str, Any]):
        self.index = index
        self.item = _norm(row.get("item_number"))
        self.refdes = _refdes(row.get("reference_designator"))
        self.mpns = _mpns(row)
        self.quantity = _number(row.get("quantity"))
        self.filled = sum(1 for value in row.values() if _norm(value))


def _lines(rows: Iterable[Mapping[str, Any]]) -> list[_Line]:
    return [_Line(index, row) for index, row in enumerate(rows)]


def _transform(pairs: list[tuple[str, str]]) -> dict[str, Any] | None:
    """Learn one item-number relation that holds one-to-one on every pair.

    Lines where either side has no item number carry no evidence and are skipped;
    at least one pair with both item numbers is required.
    """
    pairs = [(left, right) for left, right in pairs if left and right]
    if not pairs:
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


def _to_left(transform: dict[str, Any] | None, value: str) -> str | None:
    """Map a right-side item number to the left BOM's form; None without a proven relation."""
    if transform is None or not value:
        return None
    if transform["kind"] == "identical":
        return value
    affix, prefix = transform["affix"], transform["kind"] == "prefix"
    if transform["carried_by"] == "left":  # left = affix + right (or right + affix)
        return affix + value if prefix else value + affix
    if prefix:                              # right = affix + left
        return value[len(affix):] if value.startswith(affix) else None
    return value[:-len(affix)] if value.endswith(affix) else None


def _unique(lines: list[_Line], key) -> dict[Any, _Line]:
    """Index lines by key, dropping any key that is not unique within the BOM."""
    counts = Counter(k for line in lines for k in key(line))
    result = {}
    for line in lines:
        for k in key(line):
            if counts[k] == 1:
                result[k] = line
    return result


def align_boms(left_rows: Iterable[Mapping[str, Any]],
               right_rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Return relation identical / superset / subset / overlap / conflict with counts.

    superset: left contains every right line (right is redundant against left);
    subset: the reverse. overlap: neither contains the other, no conflicts.
    """
    left, right = _lines(left_rows), _lines(right_rows)
    pairs: dict[int, _Line] = {}          # right index -> left line
    taken: set[int] = set()               # left indexes already matched
    basis: Counter = Counter()

    def pair(a: _Line, b: _Line, how: str) -> None:
        pairs[b.index] = a
        taken.add(a.index)
        basis[how] += 1

    # 1. Manufacturer part number, unique within each BOM.
    left_by_mpn = _unique(left, lambda line: line.mpns)
    for line in right:
        found = {left_by_mpn[mpn].index: left_by_mpn[mpn] for mpn in line.mpns if mpn in left_by_mpn}
        if len(found) == 1:
            candidate = next(iter(found.values()))
            if candidate.index not in taken:
                pair(candidate, line, "manufacturer_part_number")
    # 2. Item number under the relation proven on the part-number matches; with
    #    no part numbers at all, only identical item numbers are trusted.
    learned = [(pairs[index].item, line.item) for index, line in enumerate(right) if index in pairs]
    transform = _transform(learned) if learned else {"kind": "identical"}
    relation_basis = ("learned_from_part_numbers" if learned and transform else
                      "not_proven" if learned else "identical_only")
    left_by_item = _unique(left, lambda line: (line.item,) if line.item else ())
    for line in right:
        if line.index in pairs:
            continue
        key = _to_left(transform, line.item)
        candidate = left_by_item.get(key) if key else None
        if candidate is not None and candidate.index not in taken:
            pair(candidate, line, "item_number")
    conflicts: list[dict[str, Any]] = []
    refdes_agree = refdes_compared = 0
    for index, a in sorted(pairs.items()):
        b = right[index]
        attribute = None
        if a.quantity and b.quantity and a.quantity != b.quantity:
            attribute = "quantity"
        elif a.mpns and b.mpns and not a.mpns & b.mpns:
            attribute = "manufacturer_part_number"
        if attribute:
            conflicts.append({"left_line": a.index + 1, "right_line": b.index + 1, "attribute": attribute})
        if a.refdes and b.refdes:
            refdes_compared += 1
            refdes_agree += a.refdes == b.refdes
    left_only = [line.index + 1 for line in left if line.index not in taken]
    right_only = [line.index + 1 for line in right if line.index not in pairs]
    if conflicts:
        relation = "conflict"
    elif not left_only and not right_only:
        relation = "identical"
    elif not right_only:
        relation = "superset"
    elif not left_only:
        relation = "subset"
    else:
        relation = "overlap"
    return {"version": VERSION, "relation": relation,
            "left_lines": len(left), "right_lines": len(right), "matched_lines": len(pairs),
            "matched_by": dict(sorted(basis.items())),
            "left_only_lines": len(left_only), "right_only_lines": len(right_only),
            "left_only_examples": left_only[:_MAX_EXAMPLES], "right_only_examples": right_only[:_MAX_EXAMPLES],
            "conflicting_lines": len(conflicts), "conflict_examples": conflicts[:_MAX_EXAMPLES],
            "item_number_relation": transform, "item_number_relation_basis": relation_basis,
            # Supporting only: counted where both matched lines carry designators.
            "refdes_compared_lines": refdes_compared, "refdes_agreeing_lines": refdes_agree}


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
        reverse = comparisons[(b, a)]["relation"]
        return {"superset": "subset", "subset": "superset"}.get(reverse, reverse)

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
    {assembly: key or None, reason, comparisons}; None unless exactly one
    assembly's BOM contains (identical or superset) every line of the undeclared BOM.
    """
    comparisons = {key: align_boms(rows, undeclared) for key, rows in sorted(declared.items())}
    containing = [key for key, value in comparisons.items() if value["relation"] in ("identical", "superset")]
    return {"version": VERSION, "assembly": containing[0] if len(containing) == 1 else None,
            "reason": ("contained_in_one_assembly" if len(containing) == 1 else
                       "contained_in_several_assemblies" if containing else "not_contained"),
            "comparisons": comparisons}
