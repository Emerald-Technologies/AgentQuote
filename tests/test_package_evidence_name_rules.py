"""Package name rules, budget-aware inventory and A059-shaped packages (QuickQuote #99).

Synthetic bytes only; no customer data or services. Run python -m pytest tests/test_package_evidence_name_rules.py.
"""
import io
import zipfile

import pytest
from openpyxl import Workbook

from bom_processor.core import CANONICAL_COLUMNS
from bom_processor.package_evidence_compact import review_compact
from bom_processor.package_evidence_contracts import LINK_NOT_TEXT_VERIFIED, PackageEvidenceError, digest
from bom_processor.package_evidence_inventory import (
    NOT_INVENTORIED_FORMAT, InventoryLimits, build_inventory, select_archives)
from bom_processor.package_evidence_name_rules import match_names
from bom_processor.package_evidence_occurrences import read_occurrences
from bom_processor.package_evidence_proposal import prepare
from compact_evidence_fixtures import QQ, V6, ScriptedModel, bom, gerber, produce, text_root, uuid


def archive(members, method=zipfile.ZIP_DEFLATED):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", method) as z:
        for name, data in members:
            z.writestr(name, data)
    return stream.getvalue()


def kit_board_package(cores=(("00001", "00011"), ("00002", "00012")), *, kit_extra=b"Assembly notes\n"):
    """Two assemblies in the approved naming family: AMT0100-<core>-TK.zip kits and AMT0110-<board>.zip boards."""
    assemblies, books, roots, index = [], {}, [], 0
    for n, (core, board) in enumerate(cores, 1):
        data = bom(3, pcb=f"0110-{board}")
        books[uuid(2, n)] = data
        assemblies.append({"assemblyUuid": uuid(1, n), "partNumber": f"0100-{core}", "revision": "A",
                           "canonicalBom": {"documentId": uuid(2, n), "sha256": digest(data), "sizeBytes": len(data)}})
        index += 1
        roots.append(text_root(archive([(f"AMT0100-{core}-TK/notes.txt", kit_extra),
                                        (f"AMT0100-{core}-TK/drawing.pdf", b"%PDF-1.4 synthetic")]),
                               f"AMT0100-{core}-TK.zip", index))
        index += 1
        roots.append(text_root(archive([(f"AMT0110-{board}/top.gbr", gerber(f"0110-{board}")),
                                        (f"AMT0110-{board}/board.drl", b"M48\nT01C0.010\n%\nM30\n"),
                                        (f"AMT0110-{board}/fab.pdf", b"%PDF-1.4 fab")]),
                               f"AMT0110-{board}.zip", index))
    index += 1
    roots.append(text_root(b"original customer BOM workbook bytes", "original.xlsx", index))
    context = {"qqUuid": QQ, "packageVersion": 1, "bomPlanId": "a" * 64, "bomApprovalId": "b" * 64,
               "resolvedManifestSha256": "c" * 64, "assemblies": assemblies}
    return context, roots, books


# ------------------------------------------------------------------ name rules

def _items(paths):
    return [{"evidenceId": f"e{i}", "format": "unknown"} for i in range(len(paths))], {f"e{i}": p for i, p in enumerate(paths)}


ASSEMBLIES = [{"assemblyUuid": "u1", "partNumber": "0100-00001", "revision": "02"},
              {"assemblyUuid": "u2", "partNumber": "0100-00002", "revision": "A"}]
OCCURRENCES = [{"assemblyUuid": "u1", "partNumber": "0110-00011", "occurrenceId": "o1"},
               {"assemblyUuid": "u2", "partNumber": "0110-00012", "occurrenceId": "o2"},
               {"assemblyUuid": "u1", "partNumber": "0110-00099", "occurrenceId": "o3"},
               {"assemblyUuid": "u2", "partNumber": "0110-00099", "occurrenceId": "o4"}]


def test_kit_board_and_bom_file_rules_map_by_name_and_core_number():
    items, paths = _items(["AMT0100-00001-TK.zip/AMT0100-00001-TK/notes.txt", "AMT0110-00012.zip/AMT0110-00012/top.gbr",
                           "AMT0110-00011.zip", "BOM-0100-00001-REV-2.xlsx", "AMT0110-00012.zip/AMT0110-00012/b.drl"])
    found = match_names(items, paths, ASSEMBLIES, OCCURRENCES)
    assert (found["e0"]["assembly"], found["e0"]["rule"], found["e0"]["occurrenceIds"], found["e0"]["role"]) == (
        "u1", "kit", [], "assembly_drawing")
    assert (found["e1"]["assembly"], found["e1"]["rule"], found["e1"]["occurrenceIds"], found["e1"]["role"]) == (
        "u2", "board", ["o2"], "artwork")
    assert (found["e2"]["assembly"], found["e2"]["role"]) == ("u1", "fabrication_drawing")
    assert (found["e3"]["assembly"], found["e3"]["rule"]) == ("u1", "bom_file")
    assert found["e4"]["role"] == "drill"


def test_any_conflict_or_unmatched_name_leaves_the_file_for_a_person():
    items, paths = _items([
        "AMT0100-00001-TK.zip/AMT0110-00012/top.gbr",    # kit of u1 holding the board of u2
        "AMT0110-00099.zip/top.gbr",                     # board on two assemblies' BOMs
        "AMT0110-00055.zip/top.gbr",                     # board on no BOM
        "AMT0100-00077-TK.zip/notes.txt",                # kit of an assembly not requested
        "BOM-0100-00002-REV-B.xlsx",                     # revision differs
        "other-package/AMT0100-00001.zip/notes.txt",     # no -TK suffix: not the approved pattern
        "drawings/0100-00001.pdf"])
    assert match_names(items, paths, ASSEMBLIES, OCCURRENCES) == {}


# ------------------------------------------------------------------ budget-aware inventory

def test_archive_over_budget_is_kept_whole_order_independently_and_bombs_still_refused():
    small = archive([("a.gbr", b"x" * 100)])
    large = archive([("b.pdf", b"y" * 900)])
    roots = [text_root(small, "small.zip", 1), text_root(large, "large.zip", 2), text_root(b"bom", "bom.xlsx", 3)]
    limits = InventoryLimits(aggregate_bytes=len(small) + len(large) + 3 + 500)
    unpack, skipped = select_archives(roots, limits)
    assert unpack == {roots[0]["documentId"]}
    assert [(s["fileName"], s["expandedBytes"], s["reason"]) for s in skipped] == [("large.zip", None, "INVENTORY_SIZE_BUDGET")]
    forward = build_inventory(roots, limits=limits)
    assert build_inventory(list(reversed(roots)), limits=limits) == forward
    kept = [item for item in forward if item["documentId"] == roots[1]["documentId"]]
    assert [(item["archiveChain"], item["format"]) for item in kept] == [([], NOT_INVENTORIED_FORMAT)]
    assert len(build_inventory(roots)) == 5  # under budget: unchanged, both archives unpacked
    bomb = archive([("bomb.bin", b"\0" * 1_000_000)])
    with pytest.raises(PackageEvidenceError, match="ARCHIVE_RATIO_LIMIT"):
        build_inventory([text_root(bomb, "bomb.zip", 1)], limits=InventoryLimits(aggregate_bytes=len(bomb) + 10))
    with pytest.raises(PackageEvidenceError, match="ARCHIVE_BYTE_LIMIT"):
        build_inventory([text_root(large, "large.zip", 1)], limits=InventoryLimits(member_bytes=899))


# ------------------------------------------------------------------ canonical workbook cells

def _workbook(revision, refdes):
    wb = Workbook()
    ws = wb.active
    ws.title = "BOM"
    ws.append(CANONICAL_COLUMNS)
    ws.append([1, "0110-00011", "PCB FABRICATION", revision, "Part", "EA", 1, refdes, None]
              + [None] * (len(CANONICAL_COLUMNS) - 9))
    stream = io.BytesIO()
    wb.save(stream)
    return stream.getvalue()


def test_numeric_revisions_and_long_designator_lists_are_read_as_quickquote_reads_them():
    refdes = ", ".join(f"C{i}" for i in range(1, 450))
    assert len(refdes) > 2000
    data = _workbook(1, refdes)
    assembly = {"assemblyUuid": uuid(1, 1), "partNumber": "0100-00001", "revision": "A",
                "canonicalBom": {"documentId": uuid(2, 1), "sha256": digest(data), "sizeBytes": len(data)}}
    rows = read_occurrences([assembly], {uuid(2, 1): data})
    occurrence = rows["occurrences"][0]
    assert (occurrence["revision"], occurrence["referenceDesignators"]) == ("1", refdes)
    data = _workbook(1.0, "R1")
    assembly["canonicalBom"] = {"documentId": uuid(2, 1), "sha256": digest(data), "sizeBytes": len(data)}
    assert read_occurrences([assembly], {uuid(2, 1): data})["occurrences"][0]["revision"] == "1"
    data = _workbook("A", "x" * 20_001)
    assembly["canonicalBom"] = {"documentId": uuid(2, 1), "sha256": digest(data), "sizeBytes": len(data)}
    with pytest.raises(PackageEvidenceError, match="INVALID_TEXT"):
        read_occurrences([assembly], {uuid(2, 1): data})


# ------------------------------------------------------------------ complete-text budget

def test_complete_text_over_budget_keeps_smallest_files_without_failing():
    sizes = [900_000, 950_000, 999_999, 800_000, 990_000, 970_000, 960_000, 940_000, 930_000]
    texts = [(f"layer{i}.gbr", (b"G04 x*\n" * (size // 7 + 1))[:size]) for i, size in enumerate(sizes)]
    context, roots, books = kit_board_package()
    roots = roots[:-1] + [text_root(archive(texts, zipfile.ZIP_STORED), "AMT0110-00011-extra.zip", 90)] + roots[-1:]

    def sources(order):
        prepared = prepare(context, order, books, producer="p", model_name="m", policy_version=V6,
                           prompt_sha256="d" * 64, run_id="0" * 64, include_classification=True,
                           include_manufacturing=True, retained_bom_sources={
                               "requestFingerprint": "e" * 64, "stagingEventId": "12345678-1234-4234-8234-123456789080",
                               "sources": [{k: roots[-1][k] for k in ("documentId", "sha256", "sizeBytes")}]})
        return prepared["sources"]
    kept = sources(roots)
    assert kept == sources(list(reversed(roots)))
    assert sum(len(s["text"].encode()) for s in kept) <= 8_000_000
    assert 999_999 not in {len(s["text"].encode()) for s in kept}


# ------------------------------------------------------------------ producer

def _proof(roots):
    source = next(r for r in roots if r["fileName"] == "original.xlsx")
    return {"requestFingerprint": "e" * 64, "stagingEventId": "12345678-1234-4234-8234-123456789080",
            "sources": [{k: source[k] for k in ("documentId", "sha256", "sizeBytes")}]}


def test_kit_board_package_is_mapped_by_code_and_the_model_only_classifies_lines():
    args = kit_board_package()
    model = ScriptedModel()
    inputs = {}
    result = produce(args, model, review_inputs=inputs)
    graph = result["proposal"]
    assert all(not p["files"] for p in model.packets)          # no file is sent to the model
    states = {d["evidenceId"]: d["state"] for d in graph["dispositions"]}
    assert "unresolved" not in states.values()
    by_assembly = {}
    for a in graph["associations"]:
        for u in a["assemblyUuids"]:
            by_assembly.setdefault(u, []).append(a)
    assert set(by_assembly) == {uuid(1, 1), uuid(1, 2)}
    board_links = [a for a in graph["associations"] if a["occurrenceIds"]]
    assert board_links and all(c["locator"] == "package name rule" for a in board_links for c in a["citations"])
    assert all(LINK_NOT_TEXT_VERIFIED in r["conflicts"] for r in result["manufacturingAssessment"]["relationships"])
    review = review_compact(inputs, graph, result["classification"], result["manufacturingAssessment"], model)
    assert review["status"] == "completed" and review["verdict"] == "needs_human" and not model.reviews
    assert all(c["verdict"] == "uncertain" for c in review["claims"])


def test_code_mapped_package_survives_a_failed_model_call_with_lines_left_open():
    args = kit_board_package()
    result = produce(args, ScriptedModel(fail_assembly="0100-00001"))
    graph = result["proposal"]
    assert "unresolved" not in {d["state"] for d in graph["dispositions"]}
    assert {c["partType"] for c in result["classification"]["classifications"]} == {"unknown"}


def test_set_aside_board_archive_is_not_retained_and_is_mapped_whole_by_the_board_rule():
    context, roots, books = kit_board_package()
    boards = {r["documentId"] for r in roots if r["fileName"].startswith("AMT0110-")}
    limits = InventoryLimits(aggregate_bytes=sum(r["sizeBytes"] for r in roots) + 9_000)
    result = produce((context, roots, books), ScriptedModel(), inventory_limits=limits)
    graph = result["proposal"]
    set_aside = {e["evidenceId"]: e for e in graph["evidenceObjects"]
                 if not e["archiveChain"] and e["category"] == NOT_INVENTORIED_FORMAT}
    assert {e["documentId"] for e in set_aside.values()} == boards
    for identity in set_aside:
        assert next(d for d in graph["dispositions"] if d["evidenceId"] == identity)["state"] == "included"
        links = [a for a in graph["associations"] if a["evidenceId"] == identity]
        assert len(links) == 1 and len(links[0]["occurrenceIds"]) == 1


class ExcludingModel(ScriptedModel):
    """Says every file is unrelated and decides no line: the worst answer for an unseen archive."""

    def propose(self, packet):
        self.packets.append(packet)
        parts = [line["id"] for line in packet["lines"] if line["cat"] == "Part"]
        subjects = [a["id"] for a in packet["assemblies"] if a["requirement"]] + parts
        return {"files": [{"file": f["id"], "state": "unrelated", "replacement": None, "rationale": "Not needed.",
                           "links": []} for f in packet["files"]],
                "parts": [{"lines": parts, "partType": "unknown", "sourceRequiresDrawing": None,
                           "sourceRequiresRevision": None, "rationale": "Unknown.", "conflicts": []}] if parts else [],
                "requirements": [{"subjects": subjects, "requiredRoles": [], "determination": "unknown",
                                  "rationale": "Unknown.", "cite": [], "conflicts": []}]}


def test_set_aside_archive_without_a_rule_is_left_for_a_person_never_excluded():
    context, roots, books = kit_board_package()
    for root in roots:
        if root["fileName"].startswith("AMT0110-"):
            root["fileName"] = root["fileName"].replace("AMT0110-", "PCB-")
    limits = InventoryLimits(aggregate_bytes=sum(r["sizeBytes"] for r in roots) + 9_000)
    graph = produce((context, roots, books), ExcludingModel(), inventory_limits=limits)["proposal"]
    set_aside = [e["evidenceId"] for e in graph["evidenceObjects"] if e["category"] == NOT_INVENTORIED_FORMAT]
    assert len(set_aside) == 2
    for identity in set_aside:
        disposition = next(d for d in graph["dispositions"] if d["evidenceId"] == identity)
        assert disposition["state"] == "unresolved" and "not unpacked" in disposition["rationale"]


def test_other_packages_are_unchanged_by_the_name_rules():
    from compact_evidence_fixtures import package
    args = package([("ASM-100200", "PCB-555001")], lines=5)
    model = ScriptedModel()
    produce(args, model)
    assert any(p["files"] for p in model.packets)
