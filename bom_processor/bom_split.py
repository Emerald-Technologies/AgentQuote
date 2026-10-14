"""Split one parsed BOM that holds several requested assemblies (QuickQuote A045, Nick 2026-10-13).

A customer BOM can list two or more of the quoted assemblies in one sheet, each with its own lines
under it (for example a Parent Part column). Each assembly's material is only its own lines.

Inputs: the parsed lines' part numbers by output row, their proven parents (output row -> parent
row, None for a top-level line) and the requested assemblies. Output: one subset per requested
assembly that heads its own tree, or None when the BOM is not such a combined BOM (today's
behaviour). Parents must already be proven from the cells (row_accounting.verify_row_map); this
module never guesses structure. Pure; run python -m pytest tests/test_bom_split.py.
"""
from __future__ import annotations

from typing import Any

from .bom_attachment import names

SPLIT_VERSION = "bom-split.v1"


class SplitError(ValueError):
    """Stable code: the BOM looks combined but its lines cannot be divided safely."""


def split_rows(item_numbers: dict[int, str], parents: dict[int, int | None],
               requested: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
    """Subsets for a combined BOM, sorted by assembly id; None when it is not one.

    It is a combined BOM when at least two top-level lines each name a different requested
    assembly (core-number rule, both ways). Then every line must sit under exactly one of those
    lines; a line under none of them, a line naming two requested assemblies, or two lines
    naming the same one is a SplitError for a person, never a guess.
    """
    rows = sorted(item_numbers)
    if not rows or set(parents) != set(rows):
        return None
    roots = [row for row in rows if parents[row] is None]
    heads: dict[int, dict[str, Any]] = {}
    for row in roots:
        hits = [item for item in requested if names(item_numbers[row], item["part_number"])]
        if len(hits) > 1:
            raise SplitError("SPLIT_LINE_AMBIGUOUS")
        if hits:
            heads[row] = hits[0]
    if len(heads) < 2:
        return None
    if len({item["assembly_id"] for item in heads.values()}) != len(heads):
        raise SplitError("SPLIT_ASSEMBLY_REPEATED")

    def top(row: int) -> int:
        seen: set[int] = set()
        while parents[row] is not None:
            if row in seen or parents[row] not in item_numbers:
                raise SplitError("SPLIT_STRUCTURE_INVALID")
            seen.add(row)
            row = parents[row]
        return row

    members: dict[int, list[int]] = {row: [] for row in heads}
    for row in rows:
        head = top(row)
        if head not in members:
            raise SplitError("SPLIT_ROW_UNASSIGNED")
        members[head].append(row)
    return [{"version": SPLIT_VERSION, "assembly_id": heads[row]["assembly_id"],
             "head_row": row, "output_rows": members[row], "line_count": len(rows)}
            for row in sorted(heads, key=lambda key: heads[key]["assembly_id"])]


def covers_once(subsets: list[dict[str, Any]]) -> bool:
    """Subsets of one BOM are for different assemblies and hold each of its lines exactly once."""
    if len(subsets) < 2 or len({item.get("assembly_id") for item in subsets}) != len(subsets):
        return False
    counts = {item.get("line_count") for item in subsets}
    if len(counts) != 1 or not isinstance(next(iter(counts)), int):
        return False
    rows = [row for item in subsets for row in item.get("output_rows", [])]
    return (all(type(row) is int for row in rows)
            and sorted(rows) == list(range(2, next(iter(counts)) + 2)))


def split_documents(groups: list[dict[str, Any]]) -> set[str] | None:
    """Documents that several groups legitimately share as proven subsets of one combined BOM.

    A document used by more than one group is accepted only when each of those groups uses it
    through exactly one fixture with a subset, and together the subsets cover its lines once.
    None when any repeat is not such a split (the caller's one-document-one-result rule stands).
    """
    uses: dict[str, list[dict[str, Any]]] = {}
    for group in groups:
        for document_id in group.get("document_ids", []):
            uses.setdefault(document_id, []).append(group)
    shared: set[str] = set()
    for document_id, members in uses.items():
        if len(members) < 2:
            continue
        subsets = []
        for group in members:
            fixtures = [item for item in (group.get("evidence") or {}).get("fixtures", [])
                        if document_id in item.get("document_ids", [])]
            if len(fixtures) != 1 or not isinstance(fixtures[0].get("subset"), dict):
                return None
            subsets.append(fixtures[0]["subset"])
        if not covers_once(subsets):
            return None
        shared.add(document_id)
    return shared
