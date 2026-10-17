"""Name-match pre-pass before the package survey (QQ #99, Nick 2026-10-14). Offline, synthetic files.

A package of loose BOM workbooks named for their assemblies plus PCB archives full of Gerber files:
each requested assembly is mapped from its file name with no model call. Any doubt falls back to
the model survey unchanged.
"""
import hashlib
import json

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
    # A second BOM-like file for one assembly that is not one of the request's BOM documents.
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


# --- QQ #99 D7: folder and zip path as a clue, at any depth -----------------------------------------

from bom_processor.package_name_match import name_match_survey, path_names  # noqa: E402
from bom_processor.package_survey import build_view, expand_package  # noqa: E402

B1 = "33333333-cccc-4ccc-8ccc-333333333333"
B2 = "44444444-dddd-4ddd-8ddd-444444444444"
REQUESTED_B = [{"assembly_id": B1, "part_number": "XYZ0100-11111-TK", "revision": "B"},
               {"assembly_id": B2, "part_number": "XYZ0100-22222-TK", "revision": "B"}]
PICK = b"Designator,X,Y,Rotation,Side\nC1,1.0,2.0,90,Top\n"
NOTES = xlsx({"Notes": [["Customer", "Due date"], ["Example Co", "2026-11-01"]]})


def prepass(files, requested=REQUESTED_B, documents=None):
    """The pre-pass alone (no survey, no model): the survey record, or None when it falls back."""
    view = build_view(expand_package(list(files.items())), [dict(item) for item in requested])
    documents = documents if documents is not None else [BOM_1, BOM_2]
    return name_match_survey(view, {hashlib.sha256(data).hexdigest() for data in documents})


def links(survey):
    """path -> requested assembly id of each linked file."""
    return {record["path"]: record["requested_assembly"]["assembly_id"] for record in survey["files"]
            if record["role"] == "bom"}


def board(folder, bom, extra=None):
    """One assembly's folder: its BOM under BOM/, Gerbers and a pick-and-place file beside it."""
    members = {f"{folder}/BOM/{bom[0]}": bom[1], f"{folder}/Gerbers/top.gbr": GERBER,
               f"{folder}/Assembly/pick and place.csv": PICK}
    members.update(extra or {})
    return members


# Folder names a person reads as the assembly: "<PN>", "<PN> Rev B", "<PN>_RevB", "ASSY <PN>".
LAYOUTS = {
    # Depth 1: each assembly's zip is named for it; the BOM is loose inside.
    "zip_name": lambda: {"XYZ0100-11111.zip": archive({"bom.xlsx": BOM_1, "top.gbr": GERBER}),
                         "0100-22222 Rev B.zip": archive({"bom.xlsx": BOM_2, "top.gbr": GERBER})},
    # Depth 3: one package zip, "<PN>" and "<PN> Rev B" folders, BOMs under BOM/.
    "folders": lambda: {"Package.zip": archive({**board("0100-11111", ("bom.xlsx", BOM_1)),
                                                **board("0100-22222 Rev B", ("bill of materials.xlsx", BOM_2))})},
    # Depth 3: "<PN>_RevB" and "ASSY <PN>" folders.
    "separators": lambda: {"Package.zip": archive({**board("0100-11111_RevB", ("bom.xlsx", BOM_1)),
                                                   **board("ASSY 0100-22222", ("parts.xlsx", BOM_2))})},
    # Depth 4 and 5: nested zips; the clue is the inner zip name for one and a folder for the other.
    "nested_zips": lambda: {"Package.zip": archive({
        "Boards/XYZ0100-11111-TK.zip": archive({"BOM/bom.xlsx": BOM_1, "Gerbers/top.gbr": GERBER}),
        "Boards/Board B.zip": archive({"Docs/0100-22222/bom.xlsx": BOM_2, "pick and place.csv": PICK})})},
}


@pytest.mark.parametrize("layout", sorted(LAYOUTS))
def test_boms_in_folders_and_nested_zips_are_matched_without_the_survey(layout):
    survey = prepass(LAYOUTS[layout]())
    assert survey is not None and survey["model_calls"] == 0 and survey["basis"] == BASIS
    linked = links(survey)
    assert sorted(linked.values()) == [B1, B2]
    assert {link["matched_in"] for link in survey["prepass"]["links"]} <= {"file_name", "folder"}
    records = {record["path"]: record for record in survey["files"]}
    assert all(records[path]["role"] == "not_surveyed" for path in records if path not in linked)
    assert not survey["has_blocking_findings"]


def test_bom_depths_one_to_four_end_to_end_without_a_model_call():
    files = {"XYZ0100-11111-TK.zip": archive({"BOM/Rev B/bom.xlsx": BOM_1}),
             "Package.zip": archive({"Inner.zip": archive({"ASSY 0100-22222/bom.xlsx": BOM_2})})}
    value = {**request(), "requested_assemblies": [dict(item) for item in REQUESTED_B]}
    result = decide(value, list(files.items()), no_model)
    assert result["model_calls"] == 0
    assert decisions(result)["doc-1"] == ("process", "only_bom", [B1])
    assert decisions(result)["doc-2"] == ("process", "only_bom", [B2])
    for depth, files in enumerate([{"0100-11111.zip": archive({"a.xlsx": BOM_1})},
                                   {"P.zip": archive({"0100-11111/a.xlsx": BOM_1})},
                                   {"P.zip": archive({"0100-11111/x/a.xlsx": BOM_1})},
                                   {"P.zip": archive({"0100-11111/x/y/a.xlsx": BOM_1})}], 1):
        files["0100-22222 BOM.xlsx"] = BOM_2
        survey = prepass(files)
        assert survey is not None, depth
        assert sorted(path.count("/") for path in links(survey)) == [0, depth]


@pytest.mark.parametrize("folder", ["0100-11111", "XYZ0100-11111-TK", "0100-11111 Rev B", "0100-11111_RevB",
                                    "0100_11111-RevB", "ASSY 0100-11111", "ASSY XYZ0100-11111 REV B",
                                    "0100 11111 rev b.zip"])
def test_path_names_reads_common_folder_names(folder):
    matches, conflict = path_names(REQUESTED_B, f"Package.zip/{folder}/BOM/bom.xlsx")
    assert not conflict and [match.requested["assembly_id"] for match in matches] == [B1]
    assert matches[0].matched_in == "folder"


@pytest.mark.parametrize("path", [
    "Package.zip/0100-11111-02/bom.xlsx",            # a sibling dash number is another part
    "Package.zip/0100-111112/bom.xlsx",              # a longer number is another part
    "Package.zip/BOM/bom.xlsx"])
def test_path_names_ignores_paths_that_do_not_name_the_assembly(path):
    assert path_names(REQUESTED_B, path) == ([], False)


def test_path_names_reports_conflicts():
    # Two assemblies on one path: by folder and file name, nested folders, or one folder naming both.
    assert path_names(REQUESTED_B, "P.zip/0100-11111/0100-22222/bom.xlsx")[1]
    assert path_names(REQUESTED_B, "P.zip/0100-11111/0100-22222 BOM.xlsx")[1]
    assert path_names(REQUESTED_B, "P.zip/0100-11111 and 0100-22222/bom.xlsx")[1]
    # A written revision that contradicts the requested one.
    assert path_names(REQUESTED_B, "P.zip/0100-11111 Rev C/bom.xlsx")[1]
    matches, conflict = path_names(REQUESTED_B, "P.zip/0100-11111 Rev B/BOM 0100-11111.xlsx")
    assert not conflict and matches[0].matched_in == "file_name" and matches[0].revision == "B"


def test_a_top_level_spreadsheet_that_names_no_assembly_no_longer_cancels_the_fast_match():
    files = dict(roots())
    files["Quote request.xlsx"] = NOTES
    result = decide(request({"doc-1": BOM_1, "doc-2": BOM_2, "doc-notes": NOTES}), list(files.items()), no_model)
    assert result["model_calls"] == 0
    assert decisions(result)["doc-notes"] == ("excluded", "not_surveyed", [])
    assert decisions(result)["doc-1"] == ("process", "only_bom", [A1])
    record = next(item for item in result["survey"]["files"] if item["path"] == "Quote request.xlsx")
    assert record["role"] == "not_surveyed"


def test_an_unnamed_spreadsheet_that_looks_like_a_bom_still_needs_the_survey_when_shallow():
    # It may be a sub-assembly or a combined BOM: the survey and a person decide.
    other = xlsx({"BOM": [HEADER] + lines(2, start=90)})
    files = {"Package.zip": archive({**board("0100-11111", ("bom.xlsx", BOM_1)),
                                     **board("0100-22222 Rev B", ("bom.xlsx", BOM_2)),
                                     "Other/BOM.xlsx": other})}
    assert prepass(files) is None
    # Deeper than every matched BOM, it is ranked lower and listed.
    files = {"Package.zip": archive({**board("0100-11111", ("bom.xlsx", BOM_1)),
                                     **board("0100-22222 Rev B", ("bom.xlsx", BOM_2)),
                                     "Other/CAD/Outputs/BOM.xlsx": other})}
    assert prepass(files) is not None


def test_two_assemblies_on_one_path_go_to_the_survey():
    # The second assembly's only BOM sits in the first one's folder: no path names it alone.
    nested = {"Package.zip": archive({"0100-11111/BOM/bom.xlsx": BOM_1,
                                      "0100-11111/0100-22222/bom.xlsx": BOM_2})}
    assert prepass(nested) is None
    # The same BOM bytes in both assemblies' folders: one file is not two assemblies' BOM here.
    shared = {"Package.zip": archive({"0100-11111/bom.xlsx": BOM_1, "0100-22222/bom.xlsx": BOM_1})}
    assert prepass(shared, documents=[BOM_1]) is None
    # A folder naming both assemblies, as shallow as the matched BOMs.
    both = {"Package.zip": archive({"0100-11111/bom.xlsx": BOM_1, "0100-22222/bom.xlsx": BOM_2,
                                    "0100-11111 and 0100-22222/bom.xlsx": xlsx({"BOM": [HEADER] + lines(1)})})}
    assert prepass(both) is None


def test_a_conflict_deeper_than_every_matched_bom_is_ranked_lower():
    files = {"Package.zip": archive({"0100-11111/bom.xlsx": BOM_1, "0100-22222/bom.xlsx": BOM_2,
                                     "0100-11111/old/0100-22222/bom.xlsx": xlsx({"BOM": [HEADER] + lines(1)})})}
    assert sorted(links(prepass(files)).values()) == [B1, B2]


MOD_1 = xlsx({"BOM": [HEADER] + lines(5)})   # the customer BOM's lines plus one: a modified BOM


class ChoiceOnly:
    """Stub model for the package choice step only: a survey call fails the test."""

    def __init__(self, primary):
        self.primary, self.calls = primary, 0

    def __call__(self, instructions, text):
        from test_bom_package_choice import CHOICE_VERSION, cell
        body, _ = json.JSONDecoder().raw_decode(text)
        assert "classify" not in body, "the model survey must not run"
        self.calls += 1
        files, primary = [], None
        for entry in body["files"]:
            disposition = "primary" if entry["path"].endswith(self.primary) else "redundant"
            primary = entry["id"] if disposition == "primary" else primary
            files.append({"file_id": entry["id"], "disposition": disposition, "rationale": "Stub.",
                          "evidence": [cell("BOM", 1, 2, "Part Number")],
                          "columns": {"sheet": "BOM", "identity_column": 2, "quantity_column": 5,
                                      "first_row": 2, "last_row": None}})
        return {"version": CHOICE_VERSION, "primary": primary, "files": files, "rationale": "Stub choice.",
                "findings": []}


def two_bom_package(customer="customer bom.xlsx", modified="modified bom.xlsm"):
    """Each assembly zip holds a customer BOM and a modified BOM at the same depth (#99 blocker 7)."""
    return {"XYZ0100-11111-TK.zip": archive({f"BOM/{customer}": BOM_1, f"BOM/{modified}": MOD_1,
                                             "Gerbers/top.gbr": GERBER}),
            "XYZ0100-22222-TK.zip": archive({"BOM/bom.xlsx": BOM_2, "Gerbers/top.gbr": GERBER})}


def test_two_bom_like_files_for_one_assembly_are_both_linked_as_candidates():
    survey = prepass(two_bom_package(), documents=[BOM_1, MOD_1, BOM_2])
    assert survey is not None and survey["model_calls"] == 0
    assert sorted(links(survey).values()) == [B1, B1, B2]
    group = next(item for item in survey["requested_assemblies"] if item["assembly_id"] == B1)
    assert len(group["files"]["bom"]) == 2
    # Two BOM-like files that are not both request documents still go to the survey.
    assert prepass(two_bom_package()) is None


def test_the_choice_step_picks_between_the_candidates_without_the_survey():
    value = {**request({"doc-1": BOM_1, "doc-1-mod": MOD_1, "doc-2": BOM_2}),
             "requested_assemblies": [dict(item) for item in REQUESTED_B]}
    model = ChoiceOnly(primary="modified bom.xlsm")
    result = decide(value, list(two_bom_package().items()), model)
    assert model.calls == 1 and result["model_calls"] == 1 and result["survey"]["model_calls"] == 0
    found = decisions(result)
    assert found["doc-1-mod"][0] == "process" and found["doc-1-mod"][2] == [B1]
    assert found["doc-1"][0] == "excluded"
    assert found["doc-2"] == ("process", "only_bom", [B2])


def test_several_same_depth_files_none_reading_like_a_bom_go_to_the_survey():
    files = {"Package.zip": archive({"0100-11111/notes.xlsx": NOTES, "0100-11111/request.xlsx": xlsx(
        {"Request": [["Contact", "Phone"], ["Example", "555"]]}), "0100-22222/bom.xlsx": BOM_2})}
    assert prepass(files) is None


def test_the_bom_like_file_wins_over_other_spreadsheets_in_the_assembly_folder():
    files = {"Package.zip": archive({**board("0100-11111", ("bom.xlsx", BOM_1), {"0100-11111/BOM/notes.xlsx": NOTES}),
                                     **board("0100-22222", ("bom.xlsx", BOM_2))})}
    assert set(links(prepass(files))) == {"Package.zip/0100-11111/BOM/bom.xlsx", "Package.zip/0100-22222/BOM/bom.xlsx"}


def test_a_folder_revision_that_contradicts_the_request_goes_to_the_survey():
    files = {"Package.zip": archive({"0100-11111 Rev A/bom.xlsx": BOM_1, "0100-22222 Rev B/bom.xlsx": BOM_2})}
    assert prepass(files) is None
    # The requested revision written in the folder is kept as the written revision.
    files = {"Package.zip": archive({"0100-11111 Rev B/bom.xlsx": BOM_1, "0100-22222 Rev B/bom.xlsx": BOM_2})}
    assert {link["file_name_revision"] for link in prepass(files)["prepass"]["links"]} == {"B"}


def test_shallow_beats_deep_and_identical_copies_count_once():
    # A loose BOM, its identical copy and an older BOM inside the board zip: the loose one is linked.
    files = {"BOM 0100-11111 REV B.xlsx": BOM_1, "0100-22222.xlsx": BOM_2,
             "XYZ0100-11111-TK.zip": archive({"BOM/bom.xlsx": BOM_1, "BOM/older bom.xlsx": xlsx(
                 {"BOM": [HEADER] + lines(3, start=60)})})}
    assert set(links(prepass(files))) == {"BOM 0100-11111 REV B.xlsx", "0100-22222.xlsx"}
    # Identical copies at one depth are one candidate; the first by path is linked.
    files = {"Package.zip": archive({"0100-11111/bom.xlsx": BOM_1, "0100-11111/bom (copy).xlsx": BOM_1,
                                     "0100-11111/copy/bom.xlsx": BOM_1, "0100-22222/bom.xlsx": BOM_2})}
    assert set(links(prepass(files))) == {"Package.zip/0100-11111/bom (copy).xlsx", "Package.zip/0100-22222/bom.xlsx"}
