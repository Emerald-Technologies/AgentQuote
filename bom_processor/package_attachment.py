"""Separate sub-assembly BOM files in package review and publication (sub-assembly plan unit 6).

QuickQuote Docs/SUBASSEMBLY_BOM_PLAN_2026-10-12.md section 13 (Nick 2026-10-13). Review: a parsed BOM
group whose assembly is named by a line of another group's BOM is folded into that group, its rows to go
under the line ("attached"), or, when the line already has its parts listed, only compared with them
("compared"). A file named by several lines is used for each. A file whose match is unclear is set aside
with a warning. Publication: the parent's rows with each attached file's rows after the naming line, and
one structure whose roll-down runs through the attached lines.

Packages with no such line keep their review, workbooks and manifest byte for byte.
Inputs/outputs: review groups and the ParentPackageService that reads their verified bytes.
Run python -m pytest tests/test_package_attachment.py.
"""
from __future__ import annotations

from typing import Any

from . import bom_attachment, bom_structure

UNCLEAR_REASON = "subassembly_unclear"
FINDING_CODES = frozenset({"SUBASSEMBLY_FILE_ATTACHED", "SUBASSEMBLY_FILE_SHARED", "SUBASSEMBLY_FILE_MATCHES",
                           "SUBASSEMBLY_FILE_DIFFERS", "SUBASSEMBLY_FILE_AMBIGUOUS",
                           "SUBASSEMBLY_FILE_REVISION_DIFFERS", "SUBASSEMBLY_FILE_NOT_RESOLVED",
                           "SUBASSEMBLY_FILE_CYCLE"})


def _text(value: Any) -> str:
    from .package_resolution import _cell_text
    return _cell_text(value)


def attach_files(service: Any, plan: dict[str, Any], groups: list[dict[str, Any]],
                 requested: list[dict[str, Any]] | None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(review groups with sub-assembly files folded in, review findings). Unchanged when none match."""
    from .package_resolution import PackageResolutionError
    ready = [group for group in groups if group.get("status") == "ready"]
    if len(ready) < 2:
        return groups, []
    nodes, views = [], {}
    for key, group in enumerate(ready):
        evidence = group["evidence"]
        identity = evidence["assembly_identity"]
        try:
            view = service._reconciled(plan, evidence)
        except PackageResolutionError as exc:
            if str(exc) != "PACKAGE_MATERIAL_CONFLICT":
                raise
            view = None  # a conflict waiting for a person's pick is neither parent nor child yet
        views[key] = view
        rows = view["rows"] if view else []
        is_requested = any(bom_attachment.names(str(item.get("part_number") or ""), identity["name"])
                           for item in requested or [])
        nodes.append(bom_attachment.Node(key, identity["name"], identity.get("revision", ""),
                                         tuple((_text(row.get("item_number")), _text(row.get("revision")))
                                               for row in rows),
                                         requested=is_requested, attachable=view is not None))
    parents_cache: dict[int, dict[int, int | None]] = {}

    def parents_of(key: int) -> dict[int, int | None]:
        if key not in parents_cache:
            view = views[key]
            parents_cache[key] = service._published_parents(plan, {"evidence": ready[key]["evidence"]}, view)[1]
        return parents_cache[key]

    def has_children(key: int, position: int) -> bool:
        return position in parents_of(key).values()

    decided = bom_attachment.plan(nodes, has_children)
    if not (decided["findings"] or decided["compare"] or decided["folded"] or decided["set_aside"]):
        return groups, []

    def descendants(key: int, position: int) -> list[int]:
        parents = parents_of(key)
        found, frontier = [], [position]
        while frontier:
            current = frontier.pop()
            for child, parent in sorted(parents.items()):
                if parent == current:
                    found.append(child)
                    frontier.append(child)
        return sorted(found)

    comparisons: dict[tuple[int, int, int], dict[str, Any]] = {}
    for parent, items in decided["compare"].items():
        for position, child in items:
            rows = views[parent]["rows"]
            listed = [(_text(rows[index].get("item_number")), rows[index].get("quantity"))
                      for index in descendants(parent, position)]
            own = bom_attachment.own_line(nodes[child])
            file_rows = [(_text(row.get("item_number")), row.get("quantity"))
                         for index, row in enumerate(views[child]["rows"]) if index != own]
            comparisons[(parent, position, child)] = bom_attachment.compare(listed, file_rows)

    def entries(key: int, trail: tuple[int, ...]) -> list[dict[str, Any]]:
        result = []
        actions = [(position, child, "attached") for position, child in decided["attach"].get(key, [])]
        actions += [(position, child, "compared") for position, child in decided["compare"].get(key, [])]
        for position, child, action in sorted(actions):
            if child in trail:
                continue
            evidence = dict(ready[child]["evidence"])
            nested = entries(child, trail + (child,))
            if nested:
                evidence["subassembly_files"] = nested
            entry = {"position": position + 1, "item_number": nodes[key].lines[position][0], "action": action,
                     "assembly_identity": ready[child]["evidence"]["assembly_identity"],
                     "document_ids": ready[child]["document_ids"], "evidence": evidence}
            if action == "compared":
                entry["comparison"] = comparisons[(key, position, child)]
            result.append(entry)
        return result

    def all_children(key: int, trail: tuple[int, ...] = ()) -> set[int]:
        found = set()
        for table in (decided["attach"], decided["compare"]):
            for _, child in table.get(key, []):
                if child not in trail and child != key:
                    found |= {child} | all_children(child, trail + (key,))
        return found

    folded, set_aside = set(decided["folded"]), set(decided["set_aside"])
    out = [group for group in groups if group.get("status") != "ready"]
    for key, group in enumerate(ready):
        if key in folded:
            continue
        if key in set_aside:
            identity = group["evidence"]["assembly_identity"]
            out.append({"kind": "excluded", "document_ids": group["document_ids"], "status": "excluded",
                        "reason": UNCLEAR_REASON, "roles": ["bom"], "assemblies": [identity["name"]],
                        "mapping_keys": group.get("mapping_keys", []), "disposition_keys": group.get("disposition_keys", [])})
            continue
        found = entries(key, (key,))
        if not found:
            out.append(group)
            continue
        below = all_children(key)
        out.append({**group, "evidence": {**group["evidence"], "subassembly_files": found},
                    "subassembly_document_ids": sorted({doc for child in below for doc in ready[child]["document_ids"]}),
                    "mapping_keys": sorted(set(group.get("mapping_keys", []))
                                           | {item for child in below for item in ready[child].get("mapping_keys", [])})})
    findings = []
    for item in decided["findings"]:
        parent, child = item["parent"], item["child"]
        findings.append({"code": item["code"], "severity": item["severity"],
                         "assembly": None if parent is None else nodes[parent].name,
                         "line": None if item["position"] is None else item["position"] + 1,
                         "item_number": None if item["position"] is None else nodes[parent].lines[item["position"]][0],
                         "file_assembly": nodes[child].name, "document_ids": ready[child]["document_ids"]})
    for (parent, position, child), result in sorted(comparisons.items()):
        findings.append({"code": "SUBASSEMBLY_FILE_MATCHES" if result["outcome"] == "matches" else "SUBASSEMBLY_FILE_DIFFERS",
                         "severity": "information" if result["outcome"] == "matches" else "warning",
                         "assembly": nodes[parent].name, "line": position + 1,
                         "item_number": nodes[parent].lines[position][0], "file_assembly": nodes[child].name,
                         "document_ids": ready[child]["document_ids"]})
    out.sort(key=lambda item: (item.get("document_ids", []), item.get("kind", "")))
    return out, findings


def expanded(service: Any, plan: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any]:
    """One BOM with its attached files: rows in published order, (row, parent row) in source order."""
    view = service._reconciled(plan, evidence)
    ref, parents, proven = service._published_parents(plan, {"evidence": evidence}, view)
    rows = view["rows"]
    attached: dict[int, list[dict[str, Any]]] = {}
    for entry in evidence.get("subassembly_files", []):
        if entry["action"] == "attached":
            attached.setdefault(entry["position"] - 1, []).append(entry)
    out_rows: list[dict[str, Any]] = []
    index: dict[int, int] = {}
    children: dict[int, list[tuple[int, int | None]]] = {}
    files: list[dict[str, Any]] = []
    for position, row in enumerate(rows):
        index[position] = len(out_rows)
        out_rows.append(row)
        for entry in attached.get(position, []):
            child = expanded(service, plan, entry["evidence"])
            name = entry["assembly_identity"]["name"]
            own = next((r for r, p in child["order"] if p is None
                        and bom_attachment.names(_text(child["rows"][r].get("item_number")), name)), None)
            proven = proven and child["proven"]
            moved: dict[int, int] = {}
            for r, item in enumerate(child["rows"]):
                if r == own:
                    continue
                moved[r] = len(out_rows)
                out_rows.append(item)
            here = index[position]
            children.setdefault(position, []).extend(
                (moved[r], here if p is None or p == own else moved[p]) for r, p in child["order"] if r != own)
            files.append({"line": here, "action": "attached", "assembly_identity": entry["assembly_identity"],
                          "document_ids": entry["document_ids"]})
            files += [{**item, "line": moved[item["line"]] if item["line"] in moved else here}
                      for item in child["files"]]
    for entry in evidence.get("subassembly_files", []):
        if entry["action"] == "compared":
            files.append({"line": index[entry["position"] - 1], "action": "compared",
                          "assembly_identity": entry["assembly_identity"], "document_ids": entry["document_ids"],
                          "comparison": entry["comparison"]})
    order: list[tuple[int, int | None]] = []
    for position in view["source_order"]:
        parent = parents.get(position)
        order.append((index[position], None if parent is None else index[parent]))
        order += children.get(position, [])
    return {"rows": out_rows, "order": order, "proven": proven, "row_map": ref, "files": files}


def structure(service: Any, plan: dict[str, Any], group: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """(published rows, structure) for a group with attached files."""
    from .package_resolution import STRUCTURE_VERSION, PackageResolutionError, _cell, _cell_text
    built = expanded(service, plan, group["evidence"])
    rows = built["rows"]
    lines = [bom_structure.Line(r + 2, _cell_text(rows[r].get("item_number")), _cell_text(rows[r].get("item_name")),
                                _cell_text(rows[r].get("item_category")), _cell(rows[r].get("quantity")),
                                any(_cell(value) is not None for key, value in rows[r].items()
                                    if str(key).startswith("manufacturer_item_number_")))
             for r, _ in built["order"]]
    if sorted(r for r, _ in built["order"]) != list(range(len(rows))):
        raise PackageResolutionError("PACKAGE_STRUCTURE_INVALID")
    parents = {r + 2: None if p is None else p + 2 for r, p in built["order"]}
    try:
        resolved = bom_structure.resolve(lines, parents, proven=built["proven"],
                                         assembly=group["evidence"]["assembly_identity"].get("name"))
    except ValueError as exc:
        raise PackageResolutionError("PACKAGE_STRUCTURE_INVALID") from exc
    out = []
    for line in resolved["lines"]:
        parent = line["parent_line"]
        item = {key: value for key, value in line.items() if key not in ("output_row", "parent_line")}
        item["line_number"] = line["output_row"] - 1
        item["parent_line_number"] = None if parent is None else parent - 1
        out.append(item)
    out.sort(key=lambda item: item["line_number"])
    ref = built["row_map"]
    files = [{**{key: value for key, value in item.items() if key != "line"}, "line_number": item["line"] + 1}
             for item in built["files"]]
    return rows, {"version": STRUCTURE_VERSION, "group_id": group["group_id"],
                  "assembly_identity": group["evidence"]["assembly_identity"],
                  "row_map_sha256": None if ref is None else ref["sha256"], "proven": built["proven"],
                  "lines": out, "findings": resolved["findings"], "summary": resolved["summary"],
                  "subassembly_files": files}
