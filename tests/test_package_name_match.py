"""Name-match pre-pass before the package survey (QQ #99, Nick 2026-10-14). Offline, synthetic files.

A package of loose BOM workbooks named for their assemblies plus PCB archives full of Gerber files:
each requested assembly is mapped from its file name with no model call. Any doubt falls back to
the model survey unchanged.
"""
import hashlib

import pytest

from bom_processor.bom_package_choice import apply_reopen, decide
from bom_processor.package_evidence_survey_clues import match_clues
from bom_processor.package_name_match import BASIS
from bom_processor.package_resolution import package_file_rows, requested_identity, requested_links
from test_bom_package_choice import HEADER, lines
from test_package_survey import archive, xlsx

A1 = "11111111-aaaa-4aaa-8aaa-111111111111"
A2 = "22222222-bbbb-4bbb-8bbb-222222222222"
REQUESTED = [{"assembly_id": A1, "part_number": "XYZ0100-11111-TK", "revision": "01"},
             {"assembly_id": A2, "part_number": "XYZ0100-22222-TK", "revision": "03"}]
BOM_1 = xlsx({"BOM": [HEADER] + lines(4)})
BOM_2 = xlsx({"BOM": [HEADER] + lines(5, start=20)})
GERBER = b"G04 synthetic*\n%FSLAX24Y24*%\nM02*\n"
DEEP_SHEET = b"Ref,X,Y\nC1,1,2\n"
PCB_1 = archive({"XYZ0100-11111/fab/top.gbr": GERBER, "XYZ0100-11111/fab/bottom.gbr": GERBER,
                 "XYZ0100-11111/assy/pick.csv": DEEP_SHEET})
PCB_2 = archive({"XYZ0100-22222/fab/top.gbr": GERBER + b"G04 two*\n",
                 "XYZ0100-22222/BOM 0100-22222 REV 03.xlsx": xlsx({"BOM": [HEADER] + lines(2, start=70)})})


def roots(**extra):
    files = {"BOM 0100-11111 REV 01.xlsx": BOM_1, "BOM 0100-22222 REV 03.xlsx": BOM_2,
             "XYZ0100-11111-TK.zip": PCB_1, "XYZ0100-22222-TK.zip": PCB_2}
    files.update(extra)
    return [(name, data) for name, data in files.items() if data is not None]


def request(documents=None):
    documents = documents if documents is not None else {"doc-1": BOM_1, "doc-2": BOM_2, "doc-deep": DEEP_SHEET}
    return {"request_id": "f" * 64, "requested_assemblies": [dict(item) for item in REQUESTED],
            "documents": [{"document_id": key, "blob_path": f"src/{key}", "size_bytes": len(value),
                           "sha256": hashlib.sha256(value).hexdigest()} for key, value in documents.items()]}


class Model:
    """Stub model that records each call and fails it (the survey turns that into a stable error)."""

    def __init__(self):
        self.calls = 0

    def __call__(self, instructions, text):
        self.calls += 1
        raise RuntimeError("model called")


def no_model(instructions, text):
    raise AssertionError("the pre-pass must not call the model")


def falls_back(value, files):
    model = Model()
    with pytest.raises(Exception):
        decide(value, files, model)
    assert model.calls > 0, "the model survey was not run"


def decisions(result):
    return {item["document_id"]: (item["decision"], item["reason"], item.get("requested_assembly_ids"))
            for item in result["documents"]}


def test_the_pre_pass_maps_named_top_level_spreadsheets_without_a_model_call():
    result = decide(request(), roots(), no_model)
    survey = result["survey"]
    assert result["model_calls"] == 0 and survey["model_calls"] == 0 and survey["basis"] == BASIS
    assert decisions(result) == {"doc-1": ("process", "only_bom", [A1]), "doc-2": ("process", "only_bom", [A2]),
                                 "doc-deep": ("excluded", "not_surveyed", [])}
    records = {item["path"]: item for item in survey["files"]}
    link = records["BOM 0100-11111 REV 01.xlsx"]["requested_assembly"]
    assert link["link"] == "confirmed" and link["basis"] == BASIS and link["assembly_id"] == A1
    assert link["matched"] == {"text": "0100-11111", "source": "path"}
    assert records["BOM 0100-22222 REV 03.xlsx"]["assembly"] == {"name": "0100-22222", "revision": "03"}
    others = [item for item in survey["files"] if item["path"] not in
              ("BOM 0100-11111 REV 01.xlsx", "BOM 0100-22222 REV 03.xlsx")]
    assert others and all(item["role"] == "not_surveyed" and item["requested_assembly"] is None for item in others)
    assert not survey["has_blocking_findings"]
    assert {item["code"] for item in survey["findings"]} >= {"SURVEY_NAME_MATCH", "SURVEY_FILES_NOT_SURVEYED"}
    assert [group["files"]["bom"] for group in survey["requested_assemblies"]] == [
        [records["BOM 0100-11111 REV 01.xlsx"]["file_id"]], [records["BOM 0100-22222 REV 03.xlsx"]["file_id"]]]
    assert survey["prepass"]["links"][0]["file_name_revision"] == "01"
    assert not [item for item in result["findings"] if item["severity"] != "information"]


def test_downstream_reads_the_pre_pass_links_like_survey_links():
    result = decide(request(), roots(), no_model)
    # Package review identity: the confirmed link gives the requested assembly (no assembly line parsed).
    identity = requested_identity(result, ["doc-1"], [], None)
    assert (identity.name, identity.revision) == ("XYZ0100-11111-TK", "01")
    group = {"group_id": "g1", "status": "ready", "document_ids": ["doc-2"],
             "evidence": {"assembly_identity": {"name": "XYZ0100-22222-TK", "revision": "03"}}}
    linked = requested_links([group], result)[0]["evidence"]["requested_link"]
    assert linked["assembly_id"] == A2 and "file or folder name" in linked["reason"]
    rows = {row["path"]: row for row in package_file_rows(result)}
    assert rows["BOM 0100-11111 REV 01.xlsx"]["classification_status"] == "classified"
    assert rows["BOM 0100-11111 REV 01.xlsx"]["link"] == "confirmed"
    deep = rows["XYZ0100-11111-TK.zip/XYZ0100-11111/assy/pick.csv"]
    assert (deep["classification_status"], deep["decision"], deep["reason"]) == ("unclassified", "excluded", "not_surveyed")
    assert rows["XYZ0100-11111-TK.zip/XYZ0100-11111/fab/top.gbr"]["decision"] == "not_parsed"
    # A person can bring back a document the pre-pass did not survey.
    state = {"status": "completed", "result": result}
    assert decisions(apply_reopen(state, ["doc-deep"])["result"])["doc-deep"][:2] == ("process", "reopened")
    # Evidence clues: a file that was not surveyed carries no clue.
    inventory = [{"evidenceId": "e1", "documentId": "d1", "sha256": hashlib.sha256(PCB_1).hexdigest(),
                  "archiveChain": [{"memberPath": "XYZ0100-11111/fab/top.gbr",
                                    "sha256": hashlib.sha256(GERBER).hexdigest()}]}]
    assert match_clues(inventory, {"d1": "XYZ0100-11111-TK.zip"}, result["survey"]["files"], {A1, A2}) == {}


def test_files_inside_an_archive_are_ignored_by_the_pre_pass():
    # PCB_2 holds a spreadsheet naming 0100-22222 with its revision; it is neither a second claim
    # nor a candidate, and the loose top-level BOM is still the link.
    result = decide(request(), roots(), no_model)
    records = {item["path"]: item for item in result["survey"]["files"]}
    deep = records["XYZ0100-22222-TK.zip/XYZ0100-22222/BOM 0100-22222 REV 03.xlsx"]
    assert deep["role"] == "not_surveyed" and deep["requested_assembly"] is None
    # Without the loose BOM, the archive's spreadsheet does not satisfy the pre-pass: survey runs.
    falls_back(request({"doc-1": BOM_1}), roots(**{"BOM 0100-22222 REV 03.xlsx": None}))


@pytest.mark.parametrize("extra", [
    # Ambiguous: one file names both requested assemblies.
    {"BOM 0100-11111 and 0100-22222.xlsx": xlsx({"BOM": [HEADER] + lines(1, start=40)})},
    # A top-level spreadsheet that names no requested assembly could be anything.
    {"Quote request.xlsx": xlsx({"BOM": [HEADER] + lines(1, start=50)})},
    # Two files for one assembly.
    {"0100-11111 BOM modified.csv": b"Item,Part Number\n1,P-1\n"},
])
def test_any_doubt_falls_back_to_the_survey(extra):
    falls_back(request(), roots(**extra))


def test_a_revision_conflict_falls_back():
    falls_back(request(), roots(**{"BOM 0100-22222 REV 03.xlsx": None, "BOM 0100-22222 REV 04.xlsx": BOM_2}))


def test_a_missing_assembly_falls_back():
    falls_back(request(), roots(**{"BOM 0100-22222 REV 03.xlsx": None}))


def test_a_named_file_that_is_not_a_request_document_falls_back():
    falls_back(request({"doc-1": BOM_1}), roots())


def test_a_v1_request_without_requested_assemblies_never_uses_the_pre_pass():
    value = request()
    del value["requested_assemblies"]
    falls_back(value, roots())


def test_an_archive_that_cannot_be_opened_is_listed_and_does_not_stop_the_pre_pass():
    result = decide(request(), roots(**{"XYZ0110-99999.zip": b"PK\x03\x04 not a real archive"}), no_model)
    finding = next(item for item in result["survey"]["findings"] if item["code"] == "SURVEY_ARCHIVE_NOT_OPENED")
    record = next(item for item in result["survey"]["files"] if item["path"] == "XYZ0110-99999.zip")
    assert finding["file_ids"] == [record["file_id"]] and record["role"] == "not_surveyed"
    assert decisions(result)["doc-1"] == ("process", "only_bom", [A1])


# --- QQ #99 D5: an empty or header-only spreadsheet has nothing to read ---------------------------

@pytest.mark.parametrize("extra", [
    # A header-only copy named for assembly 1 is not a second BOM for it.
    {"0100-11111 BOM template.csv": b"Item,Part Number,Manufacturer,MPN,Qty\r\n"},
    # An empty top-level spreadsheet names no assembly, yet does not make the package doubtful.
    {"notes.csv": b""},
    {"Quote request.xlsx": xlsx({"Request": [["Customer", "Date"]], "Blank": []})},
])
def test_an_empty_or_header_only_top_level_spreadsheet_does_not_stop_the_pre_pass(extra):
    result = decide(request(), roots(**extra), no_model)
    name = next(iter(extra))
    record = next(item for item in result["survey"]["files"] if item["path"] == name)
    assert record["role"] == "not_surveyed" and not result["survey"]["has_blocking_findings"]
    assert decisions(result)["doc-1"] == ("process", "only_bom", [A1])
    assert decisions(result)["doc-2"] == ("process", "only_bom", [A2])
