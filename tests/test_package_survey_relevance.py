"""Known-unused files in the package survey (QuickQuote #134, #99 unit 3; Nick 2026-10-17).

The survey lists ODB++, design source, 3D models, duplicate CAD drawings and viewer files as not used,
each with its reason, never sends them to the model, keeps ODB++ that may be a board's only fab data,
and QuickQuote's package review rows carry the reason code. Synthetic data only.
Run python -m pytest tests/test_package_survey_relevance.py.
"""
import json

from bom_processor.bom_package_choice import decide
from bom_processor.package_file_relevance import RULES_SHA256
from bom_processor.package_resolution import package_file_rows
from test_package_name_match import BOM_1, BOM_2, GERBER, REQUESTED, archive, no_model, request, roots

ODB = b"UNITS=INCH\n"
PCB_1 = archive({"XYZ0100-11111/fab/top.gbr": GERBER, "XYZ0100-11111/fab/drill.drl": b"M48\nM30\n",
                 "XYZ0100-11111/odb/matrix/matrix": ODB, "XYZ0100-11111/odb/steps/pcb/layers/top/features": ODB + b"#top",
                 "XYZ0100-11111/Altium/Board.PcbDoc": b"altium design", "XYZ0100-11111/Board.step": b"ISO-10303-21;",
                 "XYZ0100-11111/drawings/fab.dxf": b"0\nSECTION", "XYZ0100-11111/drawings/fab.pdf": b"%PDF-1.4 fab",
                 "XYZ0100-11111/viewer/board.cvg": b"cvg"})
# Board 2 has only ODB++ data: it may be the board's only fab data, so it stays in use with a warning.
PCB_2 = archive({"XYZ0100-22222/odb/matrix/matrix": ODB, "XYZ0100-22222/assy.pdf": b"%PDF-1.4 assy"})


class Recorder:
    """A model that records what it is shown and fails (the survey then stops with a stable error)."""

    def __init__(self):
        self.texts = []

    def __call__(self, instructions, text):
        self.texts.append(text)
        raise RuntimeError("model called")


def _files(**extra):
    return roots(**{"XYZ0100-11111-TK.zip": PCB_1, "XYZ0100-22222-TK.zip": PCB_2, **extra})


def test_unused_files_are_listed_with_their_reason_and_the_boms_still_map():
    result = decide(request({"doc-1": BOM_1, "doc-2": BOM_2}), _files(), no_model)
    survey = result["survey"]
    records = {item["path"].split("/", 1)[1] if "/" in item["path"] else item["path"]: item for item in survey["files"]}
    expected = {"XYZ0100-11111/odb/matrix/matrix": "not_used_odbpp",
                "XYZ0100-11111/odb/steps/pcb/layers/top/features": "not_used_odbpp",
                "XYZ0100-11111/Altium/Board.PcbDoc": "not_used_design_source",
                "XYZ0100-11111/Board.step": "not_used_3d_model", "XYZ0100-11111/drawings/fab.dxf": "not_used_cad_drawing",
                "XYZ0100-11111/viewer/board.cvg": "not_used_viewer"}
    unused = {path: item["not_used"]["reason_code"] for path, item in records.items() if item["role"] == "not_used"}
    assert unused == expected
    for path in expected:
        assert records[path]["classification_status"] == "not_used"
        assert records[path]["rationale"].startswith("Not used for quoting: ")
    # Board 2's ODB++ is its only fab data: kept in use, with a warning; the Gerbers and PDFs stay in use.
    for path in ("XYZ0100-22222/odb/matrix/matrix", "XYZ0100-11111/fab/top.gbr", "XYZ0100-11111/drawings/fab.pdf"):
        assert records[path]["role"] != "not_used"
    codes = {item["code"]: item for item in survey["findings"]}
    assert codes["SURVEY_ONLY_FAB_DATA_KEPT"]["severity"] == "warning"
    assert set(codes["SURVEY_FILES_NOT_USED"]["file_ids"]) == {records[path]["file_id"] for path in expected}
    assert survey["not_used"]["rules_sha256"] == RULES_SHA256 and len(survey["not_used"]["files"]) == len(expected)
    assert not survey["has_blocking_findings"]
    # The BOMs still map from their names, and every file is still listed once.
    assert result["model_calls"] == 0 and survey["basis"] == "name_match_prepass"
    assert len(survey["files"]) == len({item["file_id"] for item in survey["files"]})
    assert [group["files"]["bom"] for group in survey["requested_assemblies"]] == [
        [records["BOM 0100-11111 REV 01.xlsx"]["file_id"]], [records["BOM 0100-22222 REV 03.xlsx"]["file_id"]]]
    # QuickQuote's package review rows: not parsed, classified, with the reason code (an existing field).
    rows = {row["path"]: row for row in package_file_rows(result)}
    row = rows["XYZ0100-11111-TK.zip/XYZ0100-11111/odb/matrix/matrix"]
    assert (row["decision"], row["reason"], row["classification_status"], row["role"]) == (
        "not_parsed", "not_used_odbpp", "classified", "not_used")


def test_unused_files_are_never_shown_to_the_model():
    model = Recorder()
    # A second spreadsheet naming the first assembly stops the name-match pre-pass, so the model survey runs.
    files = _files(**{"BOM 0100-11111 REV 01 copy.xlsx": BOM_1 + b""})
    try:
        decide(request({"doc-1": BOM_1, "doc-2": BOM_2}), files, model)
    except Exception:
        pass
    assert model.texts, "the model survey was not run"
    shown = [entry["path"] for text in model.texts for entry in json.loads(text.split('{"previous_reply_rejected"')[0])["files"]]
    assert shown and not [path for path in shown if path.endswith((".PcbDoc", ".step", ".cvg", ".dxf")) or "/odb/matrix" in path
                          and "11111" in path]
    assert any(path.endswith("XYZ0100-22222/odb/matrix/matrix") for path in shown)
    # The whole package summary is still shown for context.
    assert json.loads(model.texts[0].split('{"previous_reply_rejected"')[0])["package"]["file_count"] == 14


def test_a_package_with_nothing_unused_is_unchanged():
    plain = decide(request(), roots(), no_model)
    assert "not_used" not in plain["survey"]
    assert not [item for item in plain["survey"]["files"] if item["role"] == "not_used"]
    assert REQUESTED[0]["assembly_id"] in {group["assembly_id"] for group in plain["survey"]["requested_assemblies"]}
