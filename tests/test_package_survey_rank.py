"""Ranked package survey and not-examined files (QQ #99, Nick 2026-10-14). Offline, synthetic files.

A person looks at the shallow, BOM-named workbooks first, stops once each requested assembly has its
BOM, and never digs a CSV out of a PCB archive. A file whose survey reading is refused after its
retry is listed as not examined; the package fails only when the BOM choice is impossible or unsafe.
"""
import codecs
import hashlib
import json

import pytest

from bom_processor.bom_package_choice import PackageChoiceError, apply_reopen, decide
from bom_processor.package_resolution import PACKAGE_CHOICE_FINDING_CODES, package_file_rows
from bom_processor.package_survey import PackageFile, build_view, expand_package
from bom_processor.package_survey_rank import LIKELY_SCORE, rank_files
from test_bom_package_choice import HEADER, cell, lines
from test_package_survey import archive, xlsx

A1 = "11111111-aaaa-4aaa-8aaa-111111111111"
A2 = "22222222-bbbb-4bbb-8bbb-222222222222"
REQUESTED = [{"assembly_id": A1, "part_number": "XYZ0100-11111-TK", "revision": "01"},
             {"assembly_id": A2, "part_number": "XYZ0100-22222-TK", "revision": "03"}]
BOM_1 = xlsx({"BOM": [HEADER] + lines(4)})
BOM_2 = xlsx({"BOM": [HEADER] + lines(5, start=20)})
BOM_2_MOD = xlsx({"BOM": [HEADER] + lines(6, start=20)})
# A root workbook that names no requested assembly: the name-match pre-pass does not apply.
QUOTE = xlsx({"Request": [["Customer", "Date"], ["Synthetic customer", "2026-10-14"]]})
PICK = b"Ref,X,Y\nC1,1,2\n"
GERBER = b"G04 synthetic*\n%FSLAX24Y24*%\nM02*\n"
DEEP_BOM_2 = ("Item,Part Number,Manufacturer,MPN,Qty\n" + "".join(
    f"{n},P-{n},Maker,M-{n},1\n" for n in range(70, 73))).encode()
PCB_1 = archive({"XYZ0100-11111/fab/top.gbr": GERBER, "XYZ0100-11111/assy/pick.csv": PICK})
PCB_2 = archive({"XYZ0100-22222/fab/top.gbr": GERBER + b"G04 two*\n",
                 "XYZ0100-22222/docs/parts 0100-22222.csv": DEEP_BOM_2})
PICK_PATH = "XYZ0100-11111-TK.zip/XYZ0100-11111/assy/pick.csv"
# What the stub model reads as a BOM: file name -> (requested ref, core number in the path).
BOMS = {"BOM 0100-11111 REV 01.xlsx": ("R1", "0100-11111"), "BOM 0100-22222 REV 03.xlsx": ("R2", "0100-22222"),
        "BOM 0100-22222 REV 03 modified.xlsx": ("R2", "0100-22222"),
        "parts 0100-22222.csv": ("R2", "0100-22222")}


def roots(**extra):
    files = {"BOM 0100-11111 REV 01.xlsx": BOM_1, "BOM 0100-22222 REV 03.xlsx": BOM_2,
             "Quote request.xlsx": QUOTE, "XYZ0100-11111-TK.zip": PCB_1, "XYZ0100-22222-TK.zip": PCB_2}
    files.update(extra)
    return [(name, data) for name, data in files.items() if data is not None]


DOCUMENTS = {"doc-1": BOM_1, "doc-2": BOM_2, "doc-2-mod": BOM_2_MOD, "doc-quote": QUOTE, "doc-pick": PICK,
             "doc-deep-2": DEEP_BOM_2}


def request(files):
    """The request's BOM documents: every spreadsheet in the package (as QuickQuote sends them today)."""
    present = {item.sha256 for item in expand_package(files)}
    return {"request_id": "f" * 64, "requested_assemblies": [dict(item) for item in REQUESTED],
            "documents": [{"document_id": key, "blob_path": f"src/{key}", "size_bytes": len(value),
                           "sha256": hashlib.sha256(value).hexdigest()} for key, value in DOCUMENTS.items()
                          if hashlib.sha256(value).hexdigest() in present]}


def run(model, **extra):
    files = roots(**extra)
    return decide(request(files), files, model)


def path(value):
    return {"source": "path", "value": value}


class Surveyor:
    """Stub survey model. refuse: file names whose evidence is always miscited (as role refuse_role);
    unlinked: BOM names given no requested-assembly link.

    Since QQ #182 a refused citation is dropped, and only a role other than 'other' with no matching
    citation left refuses the file; so the stub claims a role ('work_instruction' unless stated).
    """

    def __init__(self, refuse=(), refuse_role="work_instruction", unlinked=()):
        self.refuse, self.refuse_role, self.unlinked = set(refuse), refuse_role, set(unlinked)
        self.batches = []

    def __call__(self, instructions, text):
        body, _ = json.JSONDecoder().raw_decode(text)
        assert "classify" in body, "no BOM choice call is expected in these packages"
        self.batches.append([entry["path"] for entry in body["files"]])
        files = []
        for entry in body["files"]:
            name = entry["path"].rsplit("/", 1)[-1]
            sheet = entry["sheets"][0]["name"] if entry.get("sheets") else None
            record = {"file_id": entry["id"], "possibly_shared": False, "rationale": "Stub.", "findings": [],
                      "role": "other", "evidence": [], "assembly": None, "requested_assembly": None}
            if name in self.refuse:
                # SURVEY_EVIDENCE_VALUE_CHANGED for a sheet, SURVEY_EVIDENCE_NOT_IN_PATH otherwise.
                record.update(role=self.refuse_role, evidence=[cell(sheet, 1, 1, "private-cited-value") if sheet
                                                               else path("private-cited-value")])
            elif name in BOMS:
                ref, core = BOMS[name]
                record.update(role="bom", evidence=[cell(sheet, 1, 2, "Part Number")],
                              assembly={"name": [path(core)], "revision": []})
                if name not in self.unlinked:
                    record["requested_assembly"] = {"ref": ref, "matched_core": core, "evidence": [path(core)]}
            files.append(record)
        return {"version": "package-survey-reply.v2", "files": files, "findings": []}


def decisions(result):
    return {item["document_id"]: (item["decision"], item["reason"]) for item in result["documents"]}


def examination(survey):
    by_path = {item["file_id"]: item["path"] for item in survey["files"]}
    return {by_path[item["file_id"]]: item for item in survey["examination"]["files"]}


def records(survey):
    return {item["path"]: item for item in survey["files"]}


# --- ranking ---------------------------------------------------------------------------------

def test_ranking_puts_shallow_bom_workbooks_first_and_deep_csvs_and_non_spreadsheets_last():
    view = build_view(expand_package(roots()), REQUESTED)
    ranking = rank_files(view)
    paths = {item["file_id"]: item["path"] for item in records_of(view)}
    order = [paths[item["file_id"]] for item in ranking]
    assert order == ["BOM 0100-11111 REV 01.xlsx", "BOM 0100-22222 REV 03.xlsx", "Quote request.xlsx",
                     "XYZ0100-22222-TK.zip/XYZ0100-22222/docs/parts 0100-22222.csv", PICK_PATH,
                     "XYZ0100-11111-TK.zip/XYZ0100-11111/fab/top.gbr", "XYZ0100-22222-TK.zip/XYZ0100-22222/fab/top.gbr"]
    likely = {paths[item["file_id"]] for item in ranking if item["likely"]}
    assert likely == {"BOM 0100-11111 REV 01.xlsx", "BOM 0100-22222 REV 03.xlsx", "Quote request.xlsx"}
    first = ranking[0]
    assert {"depth_0", "workbook", "bom_name", "names_requested_in_name", "bom_headings"} <= set(first["clues"])
    assert [item["rank"] for item in ranking] == list(range(1, len(ranking) + 1))


def records_of(view):
    return [{"file_id": file_id, "path": view.files[file_id].path} for file_id in view.ids]


def _ranked(files):
    view = build_view([PackageFile(tuple(name.split("|")), hashlib.sha256(data).hexdigest(), len(data), data)
                       for name, data in files], REQUESTED)
    return [view.files[item["file_id"]].path.replace("|", "/") for item in rank_files(view)], rank_files(view), view


@pytest.mark.parametrize("files, expected", [
    # Same depth and clues: a workbook before a CSV.
    ([("list.csv", PICK), ("list.xlsx", QUOTE)], ["list.xlsx", "list.csv"]),
    # Same type and clues: shallow before deep.
    ([("pkg.zip|a/b/c/notes.xlsx", QUOTE), ("pkg.zip|notes.xlsx", QUOTE + b"")], ["pkg.zip/notes.xlsx", "pkg.zip/a/b/c/notes.xlsx"]),
    # A BOM name lifts a file; a Gerber or pick-and-place folder lowers it.
    ([("pkg.zip|gerber/data.xlsx", QUOTE), ("pkg.zip|other/data.xlsx", QUOTE)], ["pkg.zip/other/data.xlsx", "pkg.zip/gerber/data.xlsx"]),
    ([("pkg.zip|x/notes.xlsx", QUOTE), ("pkg.zip|x/BOM.xlsx", QUOTE)], ["pkg.zip/x/BOM.xlsx", "pkg.zip/x/notes.xlsx"]),
    # A drawing is never ahead of a spreadsheet.
    ([("a.pdf", b"%PDF-1.4 synthetic"), ("pkg.zip|a/b/c/d.csv", PICK)], ["pkg.zip/a/b/c/d.csv", "a.pdf"]),
])
def test_ranking_clues(files, expected):
    order, _, _ = _ranked(files)
    assert order == expected


def test_depth_is_relative_to_the_shallowest_spreadsheet():
    # Everything inside one uploaded zip: its top-level workbook is as likely as a loose one.
    _, ranking, view = _ranked([("pkg.zip|BOM 0100-11111.xlsx", BOM_1), ("pkg.zip|pcb.zip|fab/pick.csv", PICK)])
    by_path = {view.files[item["file_id"]].path: item for item in ranking}
    assert by_path["pkg.zip/BOM 0100-11111.xlsx"]["likely"] and "depth_0" in by_path["pkg.zip/BOM 0100-11111.xlsx"]["clues"]
    assert not by_path["pkg.zip/pcb.zip/fab/pick.csv"]["likely"]
    assert by_path["pkg.zip/pcb.zip/fab/pick.csv"]["score"] < LIKELY_SCORE


# --- early stop --------------------------------------------------------------------------------

def test_the_survey_stops_once_every_requested_assembly_has_a_confirmed_bom():
    model = Surveyor()
    result = run(model)
    survey = result["survey"]
    # One call: the three likely files; the PCB archives' contents are never sent to the model.
    assert model.batches == [["BOM 0100-11111 REV 01.xlsx", "BOM 0100-22222 REV 03.xlsx", "Quote request.xlsx"]]
    assert result["model_calls"] == survey["model_calls"] == 1
    assert survey["examination"]["stopped_early"] and not survey["has_blocking_findings"]
    listed = examination(survey)
    assert listed[PICK_PATH]["status"] == "not_examined" and listed[PICK_PATH]["reason"] == "not_needed"
    assert listed["BOM 0100-11111 REV 01.xlsx"]["status"] == "examined" and listed["BOM 0100-11111 REV 01.xlsx"]["reason"] is None
    assert records(survey)[PICK_PATH]["role"] == "not_surveyed"
    finding = next(item for item in survey["findings"] if item["code"] == "SURVEY_FILES_NOT_SURVEYED")
    assert finding["severity"] == "information" and len(finding["file_ids"]) == 4
    assert decisions(result) == {"doc-1": ("process", "only_bom"), "doc-2": ("process", "only_bom"),
                                 "doc-quote": ("excluded", "other"),
                                 "doc-pick": ("excluded", "not_surveyed"), "doc-deep-2": ("excluded", "not_surveyed")}


def test_less_likely_files_are_surveyed_in_rank_order_while_an_assembly_lacks_a_bom():
    # Assembly 2's only BOM is a CSV inside its PCB archive: the less likely files are surveyed.
    model = Surveyor()
    result = run(model, **{"BOM 0100-22222 REV 03.xlsx": None})
    assert len(model.batches) == 2 and model.batches[1][:2] == [
        "XYZ0100-22222-TK.zip/XYZ0100-22222/docs/parts 0100-22222.csv", PICK_PATH]
    assert not result["survey"]["examination"]["stopped_early"]
    assert decisions(result)["doc-deep-2"] == ("process", "only_bom")


# --- refused readings ----------------------------------------------------------------------------

def test_an_irrelevant_deep_file_that_keeps_failing_is_not_examined_and_the_package_proceeds():
    # A058-like: a CSV deep in a PCB archive keeps miscited evidence; a Gerber file too.
    model = Surveyor(refuse={"pick.csv", "top.gbr"})
    result = run(model, **{"BOM 0100-22222 REV 03.xlsx": None})
    survey = result["survey"]
    assert not survey["has_blocking_findings"]
    assert decisions(result)["doc-1"] == ("process", "only_bom") and decisions(result)["doc-deep-2"] == ("process", "only_bom")
    assert decisions(result)["doc-pick"] == ("excluded", "not_examined")
    pick = records(survey)[PICK_PATH]
    assert (pick["role"], pick["classification_status"], pick["not_examined_reason"]) == (
        "not_examined", "not_examined", "evidence_refused")
    listed = examination(survey)[PICK_PATH]
    assert (listed["status"], listed["reason"], listed["blocks"]) == ("not_examined", "evidence_refused", None)
    # The warning names every not-examined file, and QuickQuote accepts its code and severity today.
    warning = next(item for item in result["findings"] if item["code"] == "SURVEY_FILES_UNCLASSIFIED")
    assert warning["severity"] == "warning" and warning["document_ids"] == ["doc-pick"]
    assert sorted(warning["file_paths"]) == sorted([PICK_PATH, "XYZ0100-11111-TK.zip/XYZ0100-11111/fab/top.gbr",
                                                    "XYZ0100-22222-TK.zip/XYZ0100-22222/fab/top.gbr"])
    assert warning["code"] in PACKAGE_CHOICE_FINDING_CODES
    survey_warning = next(item for item in survey["findings"] if item["code"] == "SURVEY_FILES_UNCLASSIFIED")
    assert survey_warning["failure_code"] in ("SURVEY_EVIDENCE_VALUE_CHANGED", "SURVEY_EVIDENCE_NOT_IN_PATH")
    assert "private-cited-value" not in json.dumps(survey_warning["attempt_failures"])
    # Review rows keep QuickQuote's two statuses; a person can bring the file back.
    rows = {row["path"]: row for row in package_file_rows(result)}
    assert (rows[PICK_PATH]["classification_status"], rows[PICK_PATH]["decision"], rows[PICK_PATH]["reason"]) == (
        "unclassified", "excluded", "not_examined")
    assert {row["classification_status"] for row in rows.values()} <= {"classified", "unclassified"}
    reopened = apply_reopen({"status": "completed", "result": result}, ["doc-pick"])["result"]
    assert decisions(reopened)["doc-pick"] == ("process", "reopened")


def test_a_likely_file_with_no_bom_clue_that_keeps_failing_is_not_examined():
    result = run(Surveyor(refuse={"Quote request.xlsx"}))
    assert decisions(result)["doc-quote"] == ("excluded", "not_examined")
    assert examination(result["survey"])["Quote request.xlsx"]["blocks"] is None


def failed(model, files):
    with pytest.raises(PackageChoiceError) as caught:
        decide(request(files), files, model)
    assert str(caught.value) == "PACKAGE_SURVEY_INCOMPLETE"
    survey = caught.value.survey
    finding = next(item for item in survey["findings"] if item["code"] == "SURVEY_BATCH_FAILED")
    assert finding["severity"] == "blocking"
    by_id = {item["file_id"]: item["path"] for item in survey["files"]}
    return {by_id[file_id]: reason for file_id, reason in finding["block_reasons"].items()}, survey


def test_a_failing_bom_that_would_be_chosen_still_fails_the_package():
    # Assembly 2's only BOM cannot be read: the choice for it is impossible.
    reasons, survey = failed(Surveyor(refuse={"BOM 0100-22222 REV 03.xlsx"}), roots(**{"XYZ0100-22222-TK.zip": None}))
    assert reasons == {"BOM 0100-22222 REV 03.xlsx": "assembly_without_bom"}
    assert examination(survey)["BOM 0100-22222 REV 03.xlsx"]["blocks"] == "assembly_without_bom"


def test_a_failing_competing_bom_for_a_confirmed_assembly_still_fails_the_package():
    # Assembly 2 has a readable BOM, but a second likely BOM for it could have been the chosen one.
    reasons, _ = failed(Surveyor(refuse={"BOM 0100-22222 REV 03 modified.xlsx"}),
                        roots(**{"BOM 0100-22222 REV 03 modified.xlsx": BOM_2_MOD}))
    assert reasons == {"BOM 0100-22222 REV 03 modified.xlsx": "possible_bom_candidate"}


def test_a_likely_file_the_model_called_a_bom_still_fails_the_package():
    reasons, _ = failed(Surveyor(refuse={"Quote request.xlsx"}, refuse_role="bom"), roots())
    assert reasons == {"Quote request.xlsx": "possible_bom_candidate"}


def test_every_candidate_failing_fails_the_package():
    reasons, survey = failed(Surveyor(refuse={"BOM 0100-11111 REV 01.xlsx", "BOM 0100-22222 REV 03.xlsx",
                                              "parts 0100-22222.csv"}), roots())
    assert set(reasons.values()) == {"all_bom_candidates_failed"} and len(reasons) == 3
    assert not survey["examination"]["stopped_early"]


def test_no_confirmed_link_fails_the_package():
    reasons, _ = failed(Surveyor(refuse={"BOM 0100-22222 REV 03.xlsx"}, unlinked={"BOM 0100-11111 REV 01.xlsx"}),
                        roots(**{"XYZ0100-22222-TK.zip": None}))
    assert reasons == {"BOM 0100-22222 REV 03.xlsx": "no_confirmed_bom_link"}


def test_the_choice_job_log_line_counts_examined_files_without_paths():
    from choice_job import _summary
    result = run(Surveyor(refuse={"pick.csv"}), **{"BOM 0100-22222 REV 03.xlsx": None})
    line = _summary({"request_id": "r1", "status": "completed", "result": result})
    assert line["examination"] == {"examined": 5, "not_examined": 1, "stopped_early": False, "blocking_file_ids": []}
    assert line["survey_failures"][0]["code"] == "SURVEY_FILES_UNCLASSIFIED"
    assert "pick.csv" not in json.dumps(line)


def test_the_name_match_pre_pass_records_its_examination_too():
    from test_package_name_match import no_model, request as prepass_request, roots as prepass_roots
    survey = decide(prepass_request(), prepass_roots(), no_model)["survey"]
    listed = examination(survey)
    assert survey["examination"]["basis"] == "name_match_prepass" and survey["examination"]["stopped_early"]
    assert listed["BOM 0100-11111 REV 01.xlsx"]["status"] == "examined"
    assert listed["XYZ0100-11111-TK.zip/XYZ0100-11111/assy/pick.csv"] == {
        "file_id": listed["XYZ0100-11111-TK.zip/XYZ0100-11111/assy/pick.csv"]["file_id"],
        "status": "not_examined", "reason": "not_needed"}


# --- QQ #182 transport retry inside the ranked survey ---------------------------------------------

def test_a_ranked_survey_call_with_no_reply_is_sent_again_without_using_an_attempt():
    from bom_processor.package_survey_run import CALLS_PER_BATCH, survey_view
    from test_package_survey import no_reply
    model, waits, lost = Surveyor(), [], [2]

    def flaky(instructions, text):
        if lost[0]:
            lost[0] -= 1
            raise no_reply()
        return model(instructions, text)

    view = build_view(expand_package(roots()), REQUESTED)
    survey = survey_view(view, flaky, sleep=waits.append)
    # Two lost calls, then the likely files' reply: the survey completes and stops early as before.
    assert waits == [2.0, 8.0] and survey["model_calls"] == 3 and CALLS_PER_BATCH == 4
    assert not survey["has_blocking_findings"] and survey["examination"]["stopped_early"]
    assert "private transport detail" not in json.dumps(survey)


# --- QQ #99 D5: empty or header-only spreadsheets are "nothing to read" notes, never failures ------

HEADER_ONLY = b"Item,Part Number,Manufacturer,MPN,Qty\r\n"
PCB_1_TEST = archive({"XYZ0100-11111/fab/top.gbr": GERBER, "XYZ0100-11111/assy/pick.csv": PICK,
                      "XYZ0100-11111/test/ICT/BOM 0100-11111 test points.csv": HEADER_ONLY})
HEADER_ONLY_PATH = "XYZ0100-11111-TK.zip/XYZ0100-11111/test/ICT/BOM 0100-11111 test points.csv"


def test_a_header_only_csv_deep_in_a_zip_and_an_empty_csv_are_nothing_to_read_and_never_sent():
    # Assembly 2's BOM is only inside its PCB archive, so the less likely files are surveyed too.
    # Were the empty files sent, the stub would claim them as BOMs and refuse them (blocking).
    model = Surveyor(refuse={"BOM 0100-11111 test points.csv", "empty.csv"}, refuse_role="bom")
    files = roots(**{"BOM 0100-22222 REV 03.xlsx": None, "XYZ0100-11111-TK.zip": PCB_1_TEST, "empty.csv": b""})
    value = request(files)
    # QuickQuote sends every spreadsheet as a BOM document.
    value["documents"] += [{"document_id": key, "blob_path": f"src/{key}", "size_bytes": len(data),
                            "sha256": hashlib.sha256(data).hexdigest()}
                           for key, data in (("doc-header-only", HEADER_ONLY), ("doc-empty", b""))]
    result = decide(value, files, model)
    survey = result["survey"]
    sent = [path for batch in model.batches for path in batch]
    assert HEADER_ONLY_PATH not in sent and "empty.csv" not in sent
    assert not survey["has_blocking_findings"]
    assert decisions(result)["doc-1"] == ("process", "only_bom")
    assert decisions(result)["doc-deep-2"] == ("process", "only_bom")
    assert decisions(result)["doc-header-only"] == decisions(result)["doc-empty"] == ("excluded", "other")
    note = next(item for item in survey["findings"] if item["code"] == "SURVEY_NOTHING_TO_READ")
    by_path = records(survey)
    assert note["severity"] == "information" and sorted(note["file_ids"]) == sorted(
        by_path[path]["file_id"] for path in (HEADER_ONLY_PATH, "empty.csv"))
    for path in (HEADER_ONLY_PATH, "empty.csv"):
        record = by_path[path]
        assert (record["role"], record["classification_status"]) == ("other", "classified")
        assert [item["code"] for item in record["findings"]] == ["SURVEY_NOTHING_TO_READ"]
        assert (examination(survey)[path]["status"], examination(survey)[path]["reason"]) == (
            "examined", "nothing_to_read")
    # A note, not a failure: it is not one of the package-choice findings QuickQuote shows.
    assert "SURVEY_NOTHING_TO_READ" not in PACKAGE_CHOICE_FINDING_CODES
    assert not any(item["code"] == "SURVEY_NOTHING_TO_READ" for item in result["findings"])
    rows = {row["path"]: row for row in package_file_rows(result)}
    assert rows[HEADER_ONLY_PATH]["classification_status"] == rows["empty.csv"]["classification_status"] == "classified"


def test_a_refused_citation_on_one_file_keeps_the_survey_going_with_a_note():
    # QQ #182 with QQ #99: an 'other' reading whose only citation is refused is kept with a warning;
    # a claimed role whose citation is refused twice is listed as not examined. Both BOMs are found.
    for role, status in (("other", "classified"), ("work_instruction", "not_examined")):
        result = run(Surveyor(refuse={"pick.csv"}, refuse_role=role), **{"BOM 0100-22222 REV 03.xlsx": None})
        survey = result["survey"]
        assert not survey["has_blocking_findings"], role
        assert decisions(result)["doc-1"] == decisions(result)["doc-deep-2"] == ("process", "only_bom")
        pick = records(survey)[PICK_PATH]
        assert pick.get("classification_status", "classified") == status
        codes = {item["code"] for item in pick["findings"]}
        assert ("SURVEY_EVIDENCE_CITATION_REFUSED" if role == "other" else "SURVEY_FILES_UNCLASSIFIED") in codes
        assert not any(item["severity"] == "blocking" for item in pick["findings"])


def _view_of(name, data):
    return build_view([PackageFile((name,), hashlib.sha256(data).hexdigest(), len(data), data)], REQUESTED)


NOTHING_TO_READ_CASES = [
    ("empty.csv", b"", True),
    ("bom-only.csv", b"\xef\xbb\xbf", True),
    ("header.csv", HEADER_ONLY, True),
    ("header and blank lines.csv", HEADER_ONLY + b",,,,\r\n\r\n" * 30, True),
    ("one part.csv", HEADER_ONLY + b"1,P-1,Maker,M-1,2\r\n", False),
    # The part row is below the shown top rows: the whole file decides, not the view.
    ("late part.csv", HEADER_ONLY + b"\r\n" * 40 + b"1,P-1,Maker,M-1,2\r\n", False),
    ("header.xlsx", xlsx({"BOM": [HEADER], "Notes": [], "Rev": [["Rev A"]]}), True),
    ("late part.xlsx", xlsx({"BOM": [HEADER] + [[None]] * 40 + [lines(1)[0]]}), False),
    ("part on sheet two.xlsx", xlsx({"Cover": [["Title"]], "BOM": [HEADER] + lines(1)}), False),
    ("broken.xlsx", b"PK\x03\x04 not a workbook", False),
    ("drawing.pdf", b"%PDF-1.4\n", False),
    # Byte-order marks (synthetic test kit, QuickQuote PR #232): UTF-8 and UTF-16 LE/BE are read.
    ("utf-8 bom header.csv", b"\xef\xbb\xbf" + HEADER_ONLY, True),
    ("utf-16 le header.csv", codecs.BOM_UTF16_LE + HEADER_ONLY.decode().encode("utf-16-le"), True),
    ("utf-16 be header.csv", codecs.BOM_UTF16_BE + HEADER_ONLY.decode().encode("utf-16-be"), True),
    ("utf-16 le one part.csv",
     codecs.BOM_UTF16_LE + (HEADER_ONLY + b"1,P-1,Maker,M-1,2\r\n").decode().encode("utf-16-le"), False),
]


def utf16(text, order="le"):
    """CSV bytes as Excel's 'Unicode text' and other tools write them: a byte-order mark, then UTF-16."""
    mark = codecs.BOM_UTF16_LE if order == "le" else codecs.BOM_UTF16_BE
    return mark + text.encode(f"utf-16-{order}")


@pytest.mark.parametrize("order", ["le", "be"])
def test_a_utf16_csv_bom_is_read_ranked_and_chosen(order):
    # Assembly 2's only BOM is a UTF-16 CSV inside its PCB archive (it was 'binary' before).
    deep = utf16(DEEP_BOM_2.decode(), order)
    pcb_2 = archive({"XYZ0100-22222/fab/top.gbr": GERBER + b"G04 two*\n",
                     "XYZ0100-22222/docs/parts 0100-22222.csv": deep})
    files = roots(**{"BOM 0100-22222 REV 03.xlsx": None, "XYZ0100-22222-TK.zip": pcb_2})
    value = request(files)
    value["documents"].append({"document_id": "doc-utf16", "blob_path": "src/doc-utf16", "size_bytes": len(deep),
                               "sha256": hashlib.sha256(deep).hexdigest()})
    model = Surveyor()
    result = decide(value, files, model)
    path = "XYZ0100-22222-TK.zip/XYZ0100-22222/docs/parts 0100-22222.csv"
    record = records(result["survey"])[path]
    assert record["kind"] == "spreadsheet_csv" and record["role"] == "bom"
    assert record["requested_assembly"]["link"] == "confirmed"
    assert {"csv", "bom_headings"} <= set(examination(result["survey"])[path]["clues"])
    assert decisions(result)["doc-utf16"] == ("process", "only_bom")
    assert not result["survey"]["has_blocking_findings"]


@pytest.mark.parametrize("data, kind", [
    (utf16("Item,Qty\r\n1,2\r\n"), "spreadsheet_csv"),
    (utf16("Item,Qty\r\n1,2\r\n", "be"), "spreadsheet_csv"),
    (b"\xef\xbb\xbfItem,Qty\r\n1,2\r\n", "spreadsheet_csv"),
    # No byte-order mark: UTF-16 is not guessed, and the file stays binary as before.
    ("Item,Qty\r\n".encode("utf-16-le"), "binary"),
    # A UTF-32 mark begins like the UTF-16 LE one; it is not read as UTF-16.
    ("Item,Qty\r\n".encode("utf-32"), "binary"),
    # A UTF-16 mark followed by control characters is not text.
    (codecs.BOM_UTF16_LE + "\x01\x02\x03".encode("utf-16-le"), "binary"),
], ids=["utf-16 le", "utf-16 be", "utf-8 bom", "utf-16 without mark", "utf-32", "utf-16 control characters"])
def test_csv_kind_follows_the_byte_order_mark(data, kind):
    from bom_processor.package_survey import file_kind
    assert file_kind("parts.csv", data) == kind


@pytest.mark.parametrize("name, data, expected", NOTHING_TO_READ_CASES,
                         ids=[case[0] for case in NOTHING_TO_READ_CASES])
def test_nothing_to_read_is_decided_from_the_whole_file(name, data, expected):
    from bom_processor.package_survey import nothing_to_read
    assert nothing_to_read(_view_of(name, data), "F1") is expected
