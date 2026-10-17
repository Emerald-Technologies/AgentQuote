"""Known-unused package files (QuickQuote #134, #99 unit 3; Nick 2026-10-17, D9).

The shared cases (also run by QuickQuote in Python and TypeScript), the pinned mirror files, the
evidence inventory (unused bytes do not count against the budget) and the v6 producer (unused files
are listed as not needed with their reason and never sent to the model).
Synthetic data only. Run python -m pytest tests/test_package_file_relevance.py.
"""
import hashlib
import io
import json
import zipfile
from pathlib import Path

import pytest

from bom_processor import package_file_relevance as relevance
from bom_processor.package_evidence_contracts import digest
from bom_processor.package_evidence_inventory import InventoryLimits, build_inventory
from compact_evidence_fixtures import ScriptedModel, package, produce

HERE = Path(relevance.__file__).parent
# Mirrored byte for byte (line endings normalised) in QuickQuote QuickQuoteFunctions/quote_package/
# and its tests; the rules and cases also in the UI (lib/agentquote/).
RULES_SHA256 = "fff8e8ca1dfb7746477d31d3c3e0a934a9f5dd69bea2fb55aaf81d102bf4ce50"
CASES_SHA256 = "1e9f22782f19200d683ca06744ac615f972900565f9209e38c06f9d7fcebef9c"
MODULE_SHA256 = "910412213daafeecf2ceec634c0a3bc3894acb8a1a837205b7ea4f49926a46e4"
CASES = json.loads((HERE / "package-file-relevance-cases.v1.json").read_text(encoding="utf-8"))["cases"]
U = "12345678-1234-4234-8234-123456789012"
V = "12345678-1234-4234-8234-123456789013"


def _normalised(path):
    return path.read_bytes().replace(bytes([13, 10]), bytes([10]))


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_shared_cases(case):
    results, findings = relevance.classify([path.split("/") for path in case["paths"]])
    assert {p: r["reasonCode"] for p, r in zip(case["paths"], results) if r is not None} == case["disregarded"]
    assert all(r["reason"].strip() and r["kind"] for r in results if r is not None)
    assert [{"code": f["code"], "scope": f["scope"], "count": f["count"]} for f in findings] == case["findings"]


def test_rules_are_pinned_and_every_kind_has_a_reason():
    assert relevance.RULES_SHA256 == hashlib.sha256(_normalised(HERE / relevance.RULES_FILE)).hexdigest() == RULES_SHA256
    # The cases file and the module itself are the same bytes as QuickQuote's copies.
    assert hashlib.sha256(_normalised(HERE / "package-file-relevance-cases.v1.json")).hexdigest() == CASES_SHA256
    assert hashlib.sha256(_normalised(Path(relevance.__file__))).hexdigest() == MODULE_SHA256
    covered = {code for case in CASES for code in case["disregarded"].values()}
    assert covered == {"not_used_odbpp", "not_used_design_source", "not_used_3d_model", "not_used_cad_drawing", "not_used_viewer"}
    assert all(relevance.reason_for(code) for code in covered)
    assert relevance.is_not_used("not_used_odbpp") and not relevance.is_not_used("gerber_text")


def _zip(entries):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries:
            archive.writestr(name, data)
    return stream.getvalue()


def _root(data, name, document_id):
    return {"documentId": document_id, "sha256": digest(data), "sizeBytes": len(data), "fileName": name, "data": data}


# Gerbers next to ODB++ data, Altium design source and a 3D model (incompressible, so sizes are real).
BOARD = _zip([("Gerber/top.gtl", b"G04 top*" + bytes(range(256)) * 2), ("ODB/matrix/matrix", bytes(range(256)) * 8),
              ("Altium/Board.PcbDoc", bytes(reversed(range(256))) * 6), ("Board.step", bytes(range(255, 0, -1)) * 4)])
GERBERS = _zip([("bottom.gbl", b"G04 bottom*" + bytes(range(256)) * 3)])


def test_inventory_does_not_count_unused_bytes_and_lists_them_with_their_reason():
    roots = [_root(BOARD, "Board-1.zip", U), _root(GERBERS, "Board-2.zip", V)]
    used = 520 + 779  # the two Gerber files; ODB++, design source and 3D model are not counted
    limits = InventoryLimits(aggregate_bytes=len(BOARD) + len(GERBERS) + used)
    inventory = build_inventory(roots, limits=limits)
    paths = {"/".join(step["memberPath"] for step in item["archiveChain"]): item["category"] for item in inventory}
    assert paths["ODB/matrix/matrix"] == "not_used_odbpp"
    assert paths["Altium/Board.PcbDoc"] == "not_used_design_source"
    assert paths["Board.step"] == "not_used_3d_model"
    assert paths["Gerber/top.gtl"] != "not_used_odbpp" and "bottom.gbl" in paths
    # One byte less and one archive no longer fits: it is kept whole, never failing the package.
    smaller = build_inventory(roots, limits=InventoryLimits(aggregate_bytes=len(BOARD) + len(GERBERS) + used - 1))
    assert len({item["documentId"] for item in smaller if item["archiveChain"]}) == 1


def test_inventory_keeps_odb_that_is_the_only_fab_data_and_counts_it():
    only = _zip([("odb/matrix/matrix", bytes(range(256)) * 8), ("Assembly.pdf", b"%PDF-1.4 drawing")])
    inventory = build_inventory([_root(only, "Board-3.zip", U)])
    assert not any(relevance.is_not_used(item["category"]) for item in inventory)
    # Counted: with room for less than its bytes the archive is kept whole (set aside), not unpacked.
    tight = build_inventory([_root(only, "Board-3.zip", U)], limits=InventoryLimits(aggregate_bytes=len(only) + 10))
    assert [item["archiveChain"] for item in tight] == [[]]


def test_v6_producer_marks_unused_files_not_needed_and_never_shows_them_to_the_model():
    extra = [("ODB.tgz", b"\x1f\x8b synthetic odb++ archive"), ("Housing.step", b"ISO-10303-21; synthetic"),
             ("Board.PcbDoc", b"synthetic altium design")]
    args = package([("ASM-100200", "PCB-555001")], extra_files=extra)
    model = ScriptedModel()
    result = produce(args, model)
    graph = result["proposal"]
    names = {root["documentId"]: root["fileName"] for root in args[1]}
    objects = {names[item["documentId"]]: item for item in graph["evidenceObjects"] if not item["archiveChain"]}
    dispositions = {item["evidenceId"]: item for item in graph["dispositions"]}
    for name, code in (("ODB.tgz", "not_used_odbpp"), ("Housing.step", "not_used_3d_model"),
                       ("Board.PcbDoc", "not_used_design_source")):
        item = objects[name]
        assert item["category"] == code
        assert dispositions[item["evidenceId"]]["state"] == "unrelated"
        assert dispositions[item["evidenceId"]]["rationale"].startswith("Not used for quoting: " + relevance.reason_for(code))
    shown = {view["path"] for packet in model.packets for view in packet["files"]}
    assert not shown & {"ODB.tgz", "Housing.step", "Board.PcbDoc"}
    assert "PCB-555001_top.gbr" in shown
    gerber = objects["PCB-555001_top.gbr"]
    assert dispositions[gerber["evidenceId"]]["state"] == "included"
