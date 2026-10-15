"""Stage 1 package survey (plan 8.3): view, checks and run loop. Offline, synthetic data only."""
import copy
import io
import json
import zipfile

import pytest
from openpyxl import Workbook

from bom_processor.document_role import ROLE_DEFINITIONS
from bom_processor.package_survey import (FILE_CELL_LIMIT, SURVEY_INSTRUCTIONS, SurveyError,
                                          build_view, expand_package, plan_batches)
from bom_processor.package_survey_check import verify_batch
from bom_processor.package_survey_run import survey_package


def xlsx(sheets, *, macros=False):
    book = Workbook()
    book.remove(book.active)
    for title, rows in sheets.items():
        sheet = book.create_sheet(title)
        for row in rows:
            sheet.append(row)
    stream = io.BytesIO(); book.save(stream); book.close()
    if not macros:
        return stream.getvalue()
    # A macro part is added the way Excel stores it; the survey must not open it.
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(stream.getvalue())) as source, zipfile.ZipFile(out, "w") as target:
        for item in source.infolist():
            target.writestr(item, source.read(item))
        target.writestr("xl/vbaProject.bin", b"\x00synthetic-not-a-real-macro")
    return out.getvalue()


def archive(members):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as target:
        for name, data in members.items():
            target.writestr(name, data)
    return stream.getvalue()


BOM_ROWS = [["Assembly", "ASSY-100", "Rev", "B"], [],
            ["Item", "Part Number", "Manufacturer", "MPN", "Qty", "Ref Des"],
            [1, "P-1", "Maker", "M-1", 2, "C1,C2"], [2, "P-2", "Maker", "M-2", 1, "R1"]]
MPI_ROWS = [["Manufacturing Process Instruction"], ["Assembly:ASSY-100", "Rev: B"],
            ["Step", "Operation"], [10, "Apply paste"], [20, "Place parts"],
            ["Parts used"], ["P-1", 2], ["P-2", 1]]


def a030_like():
    """Two assemblies, two candidate BOMs for A, an MPI each, PDFs and Gerber data."""
    assembly_a = archive({
        "ASSY-100 BOM.xlsx": xlsx({"BOM": BOM_ROWS}),
        "ASSY-100 MOD BOM.xlsx": xlsx({"BOM": BOM_ROWS + [[3, "P-3", "Maker", "M-3", 4, "U1"]]}),
        "ASSY-100 MPI.xlsm": xlsx({"MPI": MPI_ROWS, "Pictures": [["see layout"]]}, macros=True),
        "ASSY-100 drawing.pdf": b"%PDF-1.4 synthetic",
    })
    assembly_b = archive({
        "ASSY-200 BOM.xlsx": xlsx({"BOM": [["Assembly", "ASSY-200"]] + BOM_ROWS[2:]}),
        "ASSY-200 MPI.xlsx": xlsx({"MPI": [["Process instruction"], ["Assembly:ASSY-200"], ["Step"], [10]]}),
    })
    gerbers = archive({"fab/top.gbr": b"G04 synthetic*\n%FSLAX24Y24*%\n", "fab/drill.drl": b"M48\n"})
    return [("package.zip", archive({"A.zip": assembly_a, "B.zip": assembly_b, "PCB.zip": gerbers}))]


def by_name(view):
    return {item.chain[-1].rsplit("/", 1)[-1]: file_id for file_id, item in view.files.items()}


def cell(sheet, row, column, value):
    return {"source": "cell", "value": value, "cell": {"sheet": sheet, "row": row, "column": column}}


def path(value):
    return {"source": "path", "value": value}


def record(file_id, role, *, assembly=None, evidence=(), shared=False, findings=()):
    return {"file_id": file_id, "role": role, "assembly": assembly, "possibly_shared": shared,
            "rationale": "Synthetic stub rationale.", "evidence": list(evidence), "findings": list(findings)}


def a030_answers(view):
    ids = by_name(view)
    a = {"name": [cell("BOM", 1, 2, "ASSY-100"), path("ASSY-100")], "revision": [cell("BOM", 1, 4, "B")]}
    mpi_a = {"name": [cell("MPI", 2, 1, "ASSY-100")], "revision": [cell("MPI", 2, 2, "B")]}
    b = {"name": [cell("BOM", 1, 2, "ASSY-200")], "revision": []}
    answers = {
        ids["ASSY-100 BOM.xlsx"]: record(ids["ASSY-100 BOM.xlsx"], "bom", assembly=a,
                                         evidence=[cell("BOM", 3, 2, "Part Number"), cell("BOM", 3, 6, "Ref Des")]),
        ids["ASSY-100 MOD BOM.xlsx"]: record(ids["ASSY-100 MOD BOM.xlsx"], "bom", assembly=a,
                                             evidence=[cell("BOM", 3, 4, "MPN")]),
        ids["ASSY-100 MPI.xlsm"]: record(ids["ASSY-100 MPI.xlsm"], "work_instruction", assembly=mpi_a,
                                         evidence=[cell("MPI", 1, 1, "Manufacturing Process Instruction"),
                                                   cell("MPI", 3, 2, "Operation")]),
        ids["ASSY-100 drawing.pdf"]: record(ids["ASSY-100 drawing.pdf"], "other",
                                            assembly={"name": [path("ASSY-100")], "revision": []}),
        ids["ASSY-200 BOM.xlsx"]: record(ids["ASSY-200 BOM.xlsx"], "bom", assembly=b,
                                         evidence=[cell("BOM", 2, 2, "Part Number")]),
        ids["ASSY-200 MPI.xlsx"]: record(ids["ASSY-200 MPI.xlsx"], "work_instruction",
                                         assembly={"name": [cell("MPI", 2, 1, "ASSY-200")], "revision": []},
                                         evidence=[cell("MPI", 1, 1, "Process instruction")]),
        ids["top.gbr"]: record(ids["top.gbr"], "other", shared=True),
        ids["drill.drl"]: record(ids["drill.drl"], "other", shared=True),
    }
    return answers


class StubAgent:
    """Answers each batch from a fixed per-file table; records what it was sent."""

    def __init__(self, answers, mutate=None):
        self.answers, self.mutate, self.sent = answers, mutate, []

    def __call__(self, instructions, user_text):
        self.sent.append((instructions, user_text))
        batch, _ = json.JSONDecoder().raw_decode(user_text)
        reply = {"version": "package-survey-reply.v1",
                 "files": [copy.deepcopy(self.answers[file_id]) for file_id in batch["classify"]],
                 "findings": []}
        return self.mutate(reply, len(self.sent)) if self.mutate else reply


def run(roots=None, answers=None, **kwargs):
    roots = roots or a030_like()
    view = build_view(expand_package(roots))
    agent = StubAgent(answers or a030_answers(view), kwargs.pop("mutate", None))
    return view, agent, survey_package(roots, agent, **kwargs)


@pytest.mark.parametrize("evidence,code", [
    ("private-response-value", "SURVEY_EVIDENCE_RECORD_INVALID"),
    ({"source": "private-response-value", "value": "x"}, "SURVEY_EVIDENCE_SOURCE_INVALID"),
    ({"source": "path", "value": "x", "extra": "private-response-value"}, "SURVEY_EVIDENCE_FIELDS_INVALID"),
    ({"source": "cell", "value": "x"}, "SURVEY_EVIDENCE_FIELDS_INVALID"),
    ({"source": "cell", "value": 17, "cell": {}}, "SURVEY_EVIDENCE_VALUE_TYPE_INVALID"),
    ({"source": "path", "value": None}, "SURVEY_EVIDENCE_VALUE_TYPE_INVALID"),
    ({"source": "path", "value": ""}, "SURVEY_EVIDENCE_VALUE_EMPTY"),
    ({"source": "path", "value": "private-response-value" * 20}, "SURVEY_EVIDENCE_VALUE_LIMIT"),
    ({"source": "path", "value": "   "}, "SURVEY_EVIDENCE_VALUE_EMPTY"),
])
def test_evidence_rejection_names_rule_without_leaking_reply_or_adding_retries(evidence, code):
    roots = [("bom.xlsx", xlsx({"BOM": BOM_ROWS}))]
    calls = []
    def agent(_instructions, user_text):
        calls.append(user_text)
        batch, _ = json.JSONDecoder().raw_decode(user_text)
        return {"version": "package-survey-reply.v1", "files": [
            record(batch["classify"][0], "bom", evidence=[copy.deepcopy(evidence)])], "findings": []}
    result = survey_package(roots, agent)
    assert len(calls) == result["model_calls"] == 2
    assert code in calls[1] and "private-response-value" not in calls[1]
    assert result["has_blocking_findings"]
    finding = next(f for f in result["findings"] if f["code"] == "SURVEY_BATCH_FAILED")
    assert finding["failure_code"] == code and finding["raised_by"] == "code"
    assert all(f["classification_status"] == "unclassified" for f in result["files"])
    assert "private-response-value" not in json.dumps(result)


def test_every_file_is_recorded_including_non_spreadsheets_and_nested_archives():
    view = build_view(expand_package(a030_like()))
    kinds = {item.chain[-1].rsplit("/", 1)[-1]: view.entries[file_id]["kind"] for file_id, item in view.files.items()}
    assert kinds == {"ASSY-100 BOM.xlsx": "spreadsheet_ooxml", "ASSY-100 MOD BOM.xlsx": "spreadsheet_ooxml",
                     "ASSY-100 MPI.xlsm": "spreadsheet_ooxml", "ASSY-100 drawing.pdf": "pdf",
                     "ASSY-200 BOM.xlsx": "spreadsheet_ooxml", "ASSY-200 MPI.xlsx": "spreadsheet_ooxml",
                     "top.gbr": "gerber_text", "drill.drl": "excellon_text"}
    paths = sorted(item.path for item in view.files.values())
    assert "package.zip/PCB.zip/fab/top.gbr" in paths


def test_a030_shaped_package_gives_roles_and_assemblies_from_plan_8_7():
    view, agent, survey = run()
    assert survey["model_calls"] == 1 and len(survey["files"]) == 8
    groups = {group["name"]: group for group in survey["assemblies"]}
    ids = by_name(view)
    assert sorted(groups["ASSY-100"]["files"]["bom"]) == sorted([ids["ASSY-100 BOM.xlsx"], ids["ASSY-100 MOD BOM.xlsx"]])
    assert groups["ASSY-100"]["files"]["work_instruction"] == [ids["ASSY-100 MPI.xlsm"]]
    assert groups["ASSY-200"]["files"] == {"bom": [ids["ASSY-200 BOM.xlsx"]],
                                           "work_instruction": [ids["ASSY-200 MPI.xlsx"]], "other": []}
    assert sorted(survey["unassigned"]) == sorted([ids["top.gbr"], ids["drill.drl"]])
    assert not survey["has_blocking_findings"]
    instructions, text = agent.sent[0]
    assert instructions == SURVEY_INSTRUCTIONS and ROLE_DEFINITIONS in instructions
    assert "Apply paste" in text and "macros_present_not_opened" in text


def test_macro_workbook_is_read_without_opening_the_macro_part():
    view, _, survey = run()
    mpi = next(item for item in survey["files"] if item["path"].endswith("MPI.xlsm"))
    assert view.entries[mpi["file_id"]]["macros_present_not_opened"] is True
    assert "SURVEY_MACROS_NOT_OPENED" in {finding["code"] for finding in mpi["findings"]}


def test_work_instruction_with_parts_table_keeps_the_agent_role():
    view, _, survey = run()
    mpi = next(item for item in survey["files"] if item["path"].endswith("MPI.xlsm"))
    assert mpi["role"] == "work_instruction"
    assert mpi["assembly"] == {"name": "ASSY-100", "revision": "B"}
    # The cited 'ASSY-100' is a whole token inside 'Assembly:ASSY-100'.
    assert mpi["assembly_evidence"]["name"][0]["cell"] == {"sheet": "MPI", "row": 2, "column": 1}


def test_file_with_no_assembly_gets_a_warning_not_a_block():
    _, _, survey = run()
    gerber = next(item for item in survey["files"] if item["path"].endswith("top.gbr"))
    finding = next(item for item in gerber["findings"] if item["code"] == "SURVEY_ASSEMBLY_NOT_IDENTIFIED")
    assert finding["severity"] == "warning" and not survey["has_blocking_findings"]


def test_name_that_disagrees_with_contents_is_a_finding():
    roots = [("package.zip", archive({"ASSY-999 BOM.xlsx": xlsx({"BOM": BOM_ROWS})}))]
    view = build_view(expand_package(roots))
    file_id = view.ids[0]
    answers = {file_id: record(file_id, "bom", evidence=[cell("BOM", 3, 2, "Part Number")],
                               assembly={"name": [cell("BOM", 1, 2, "ASSY-100"), path("ASSY-999")], "revision": []})}
    _, _, survey = run(roots, answers)
    item = survey["files"][0]
    assert item["assembly"]["name"] == "ASSY-100"
    assert "NAME_CONTENT_DISAGREE" in {finding["code"] for finding in item["findings"]}


@pytest.mark.parametrize("change, code", [
    (lambda r: r["evidence"].__setitem__(0, cell("BOM", 3, 2, "Invented")), "SURVEY_EVIDENCE_VALUE_CHANGED"),
    (lambda r: r["evidence"].__setitem__(0, cell("BOM", 99, 2, "Part Number")), "SURVEY_EVIDENCE_CELL_NOT_SHOWN"),
    (lambda r: r["evidence"].__setitem__(0, path("ASSY-555")), "SURVEY_EVIDENCE_NOT_IN_PATH"),
    # A whole word inside the cell may be cited ('Part' of 'Part Number'); a partial word may not.
    (lambda r: r["evidence"].__setitem__(0, cell("BOM", 3, 2, "Numb")), "SURVEY_EVIDENCE_VALUE_CHANGED"),
    (lambda r: r.update(evidence=[]), "SURVEY_ROLE_EVIDENCE_REQUIRED"),
    (lambda r: r.update(role="drawing"), "SURVEY_RECORD_INVALID"),
    (lambda r: r.update(assembly={"name": [], "revision": []}), "SURVEY_ASSEMBLY_INVALID"),
    (lambda r: r["findings"].append({"code": "X", "severity": "fatal", "rationale": "r"}), "SURVEY_FINDING_INVALID"),
])
def test_cited_evidence_is_checked_against_the_shown_cells_and_path(change, code):
    view = build_view(expand_package(a030_like()))
    answers = a030_answers(view)
    batch = plan_batches(view)[0]
    target = answers[by_name(view)["ASSY-100 BOM.xlsx"]]
    change(target)
    reply = {"version": "package-survey-reply.v1", "files": [answers[i] for i in batch.file_ids], "findings": []}
    with pytest.raises(SurveyError, match=code):
        verify_batch(view, batch, reply)


def test_every_file_needs_exactly_one_record():
    view = build_view(expand_package(a030_like()))
    answers = a030_answers(view)
    batch = plan_batches(view)[0]
    files = [answers[i] for i in batch.file_ids]
    for broken in (files[1:], files + [files[0]]):
        with pytest.raises(SurveyError, match="SURVEY_COVERAGE_INVALID"):
            verify_batch(view, batch, {"version": "package-survey-reply.v1", "files": broken, "findings": []})


def test_a_rejected_reply_is_retried_once_with_the_code_only():
    def mutate(reply, attempt):
        if attempt == 1:
            reply["files"] = reply["files"][1:]
        return reply
    _, agent, survey = run(mutate=mutate)
    assert survey["model_calls"] == 2
    assert '"previous_reply_rejected":"SURVEY_COVERAGE_INVALID"' in agent.sent[1][1]


def test_two_rejected_replies_leave_unclassified_files_with_the_code():
    def mutate(reply, _):
        reply["version"] = "wrong"
        return reply
    _, agent, survey = run(mutate=mutate)
    assert survey["model_calls"] == 2 and survey["has_blocking_findings"]
    assert all(item["classification_status"] == "unclassified" for item in survey["files"])
    assert survey["findings"][0]["failure_code"] == "SURVEY_REPLY_INVALID"


def test_call_ceiling_stops_further_calls():
    def mutate(reply, _):
        reply["files"] = []
        return reply
    _, agent, survey = run(mutate=mutate, max_calls=1)
    assert survey["model_calls"] == 1 and len(agent.sent) == 1
    assert survey["has_blocking_findings"] and survey["findings"][0]["code"] == "SURVEY_CALL_LIMIT"


def test_large_package_is_split_into_batches_with_every_file_once():
    rows = [["Header", "Value"]] + [[f"row {index}", "x" * 150] for index in range(1, 20)]
    members = {f"folder{index // 10}/sheet{index}.xlsx": xlsx({"S": rows}) for index in range(30)}
    members.update({f"docs/file{index}.pdf": b"%PDF-1.4" for index in range(50)})
    view = build_view(expand_package([("big.zip", archive(members))]))
    batches = plan_batches(view)
    assert len(batches) > 1
    seen = [file_id for batch in batches for file_id in batch.file_ids]
    assert sorted(seen) == sorted(view.ids) and len(seen) == len(set(seen))
    assert all(len(batch.user_text) <= 60_000 and len(batch.file_ids) <= 40 for batch in batches)


def test_view_bounds_cells_and_redacts_links_and_long_tokens():
    wide = [[f"v{row}-{column}" for column in range(1, 60)] for row in range(1, 21)]
    long_value = "word " * 99 + "end"
    secret = "see https://example.invalid/x and " + "Z" * 40
    roots = [("p.zip", archive({"wide.xlsx": xlsx({"S": wide}), "long.xlsx": xlsx({"S": [[long_value, secret]]})}))]
    view = build_view(expand_package(roots))
    ids = by_name(view)
    wide_entry = view.entries[ids["wide.xlsx"]]
    assert wide_entry["view_limited"] is True
    assert sum(len(cells) for _, cells in wide_entry["sheets"][0]["top"]) <= FILE_CELL_LIMIT
    shown = view.entries[ids["long.xlsx"]]["sheets"][0]["top"][0][1]
    assert shown[0][1].endswith("\u2026") and len(shown[0][1]) == 201
    assert "example.invalid" not in shown[1][1] and "Z" * 40 not in shown[1][1]
    # Citations are checked against the original value, not the shortened display.
    file_id = ids["long.xlsx"]
    batch = next(b for b in plan_batches(view) if file_id in b.file_ids)
    answers = {i: record(i, "other") for i in batch.file_ids}
    answers[file_id] = record(file_id, "other", evidence=[cell("S", 1, 1, long_value[:256])])
    with pytest.raises(SurveyError, match="SURVEY_EVIDENCE_VALUE_CHANGED"):
        verify_batch(view, batch, {"version": "package-survey-reply.v1",
                                   "files": [answers[i] for i in batch.file_ids], "findings": []})


def test_unreadable_archive_and_workbook_are_recorded_not_dropped():
    roots = [("p.zip", archive({"broken.zip": b"PK\x03\x04 not a zip", "bad.xlsx": b"PK\x03\x04 nope",
                                "notes.txt": b"plain text"}))]
    view = build_view(expand_package(roots))
    entries = {item.chain[-1]: view.entries[file_id] for file_id, item in view.files.items()}
    assert entries["broken.zip"]["kind"] == "archive_unreadable" and entries["broken.zip"]["unreadable"]
    assert entries["bad.xlsx"]["unreadable"] == "WORKBOOK_INVALID"
    assert entries["notes.txt"]["kind"] == "text"


def test_identical_files_are_linked_and_a_different_verdict_is_reported():
    data = xlsx({"BOM": BOM_ROWS})
    roots = [("p.zip", archive({"one/BOM.xlsx": data, "two/BOM.xlsx": data}))]
    view = build_view(expand_package(roots))
    first, second = view.ids
    assert view.entries[first]["same_bytes_as"] == [second]
    answers = {first: record(first, "bom", evidence=[cell("BOM", 3, 2, "Part Number")]),
               second: record(second, "other")}
    _, _, survey = run(roots, answers)
    assert "SAME_BYTES_DIFFERENT_ROLE" in {finding["code"] for finding in survey["findings"]}


def test_csv_and_html_xls_top_rows_are_shown():
    html = b"<html><body><table><tr><td>Part</td><td>Qty</td></tr><tr><td>P-1</td><td>2</td></tr></table></body></html>"
    roots = [("p.zip", archive({"a.csv": b"Part,Qty\nP-1,2\n", "b.xls": html}))]
    view = build_view(expand_package(roots))
    entries = {item.chain[-1]: view.entries[file_id] for file_id, item in view.files.items()}
    assert entries["a.csv"]["sheets"][0]["top"][0] == [1, [[1, "Part"], [2, "Qty"]]]
    assert entries["b.xls"]["kind"] == "spreadsheet_html_xls"
    assert entries["b.xls"]["sheets"][0]["top"][1] == [2, [[1, "P-1"], [2, "2"]]]


def test_roots_are_validated():
    for roots in ([], [("a/b.zip", b"")], [("x.zip", "text")], [("a.zip", b""), ("A.ZIP", b"")]):
        with pytest.raises(SurveyError):
            expand_package(roots)


SPACED_ROOTS = [("Pkg.zip", archive({"FAB DWG ASSY-100 REV-4.xlsx": xlsx({"BOM": BOM_ROWS})}))]


def single_file_agent(**fields):
    def agent(_instructions, user_text):
        batch, _ = json.JSONDecoder().raw_decode(user_text)
        return {"version": "package-survey-reply.v1",
                "files": [record(batch["classify"][0], "bom", **copy.deepcopy(fields))], "findings": []}
    return agent


def test_evidence_ignores_surrounding_spaces_case_and_spacing_and_records_source_text():
    agent = single_file_agent(
        assembly={"name": [path(" assy-100 "), cell("BOM", 1, 2, " assy-100")], "revision": [path("rev-4")]},
        evidence=[path("fab  dwg"), cell("BOM", 3, 2, "part number ")])
    result = survey_package(SPACED_ROOTS, agent)
    assert result["model_calls"] == 1 and not result["has_blocking_findings"]
    item = result["files"][0]
    assert item["role"] == "bom" and item["assembly"] == {"name": "ASSY-100", "revision": "REV-4"}
    assert [e["value"] for e in item["evidence"]] == ["FAB DWG", "Part Number"]
    assert [e["value"] for e in item["assembly_evidence"]["name"]] == ["ASSY-100", "ASSY-100"]


@pytest.mark.parametrize("evidence,code", [
    (path("SSY-100"), "SURVEY_EVIDENCE_NOT_IN_PATH"),
    (path("ASSY-1000"), "SURVEY_EVIDENCE_NOT_IN_PATH"),
    (path("FABDWG"), "SURVEY_EVIDENCE_NOT_IN_PATH"),
    (cell("BOM", 1, 2, "SSY-100"), "SURVEY_EVIDENCE_VALUE_CHANGED"),
])
def test_tolerant_matching_still_refuses_partial_or_absent_text(evidence, code):
    result = survey_package(SPACED_ROOTS, single_file_agent(evidence=[evidence]))
    finding = next(f for f in result["findings"] if f["code"] == "SURVEY_BATCH_FAILED")
    assert finding["failure_code"] == code and result["has_blocking_findings"]


def refused_run(name, citation, calls=(1, 2)):
    """A030-shaped run whose agent cites text that is not in the named file, on the given calls."""
    view = build_view(expand_package(a030_like()))
    target = by_name(view)[name]

    def mutate(reply, count):
        for item in reply["files"]:
            if item["file_id"] == target and count in calls:
                item["assembly"] = {"name": [copy.deepcopy(citation)], "revision": []}
        return reply
    _, agent, survey = run(mutate=mutate)
    return target, agent, survey


def retry_feedback(agent):
    batch, end = json.JSONDecoder().raw_decode(agent.sent[1][1])
    return batch["classify"], json.loads(agent.sent[1][1][end:])


def test_one_refused_drawing_keeps_the_other_files_and_does_not_block():
    pdf, agent, survey = refused_run("ASSY-100 drawing.pdf", path("private-response-value"))
    assert len(agent.sent) == survey["model_calls"] == 2
    classify, feedback = retry_feedback(agent)
    # The retry asks only about the refused file and never repeats the refused value.
    assert classify == [pdf] and feedback == {
        "previous_reply_rejected": [{"file_id": pdf, "failure_code": "SURVEY_EVIDENCE_NOT_IN_PATH"}],
        "instruction": "These records were refused; return a complete corrected reply for every id in classify."}
    assert "private-response-value" not in agent.sent[1][1]
    assert not survey["has_blocking_findings"]
    assert [f["file_id"] for f in survey["files"] if f.get("classification_status") == "unclassified"] == [pdf]
    assert {group["name"] for group in survey["assemblies"]} == {"ASSY-100", "ASSY-200"}
    finding = next(f for f in survey["findings"] if f["code"] == "SURVEY_FILES_UNCLASSIFIED")
    assert finding["severity"] == "warning" and finding["file_ids"] == [pdf]
    # Each refusal names the field it came from (round 3).
    assert finding["attempt_failures"] == [{"failure_code": "SURVEY_EVIDENCE_NOT_IN_PATH", "file_id": pdf, "field": "assembly"}] * 2
    # The stored result keeps what the agent wrote, so a refusal can be diagnosed.
    assert finding["rejected_citations"] == [{"file_id": pdf, "failure_code": "SURVEY_EVIDENCE_NOT_IN_PATH",
                                              "source": "path", "value": "private-response-value", "field": "assembly"}]


def test_retry_that_corrects_the_refused_file_classifies_everything():
    pdf, agent, survey = refused_run("ASSY-100 drawing.pdf", path("private-response-value"), calls=(1,))
    assert survey["model_calls"] == 2 and retry_feedback(agent)[0] == [pdf]
    assert not survey["has_blocking_findings"]
    assert not any(f.get("classification_status") == "unclassified" for f in survey["files"])
    assert not any(f["code"].startswith("SURVEY_") and f["raised_by"] == "code" and "file_ids" in f
                   for f in survey["findings"])


def test_refused_spreadsheet_still_blocks_so_every_bom_is_retained():
    bom, _, survey = refused_run("ASSY-200 BOM.xlsx", cell("BOM", 1, 2, "private-response-value"))
    assert survey["has_blocking_findings"]
    finding = next(f for f in survey["findings"] if f["code"] == "SURVEY_BATCH_FAILED")
    assert finding["severity"] == "blocking" and finding["file_ids"] == [bom]
    assert finding["failure_code"] == "SURVEY_EVIDENCE_VALUE_CHANGED"
    assert finding["rejected_citations"][0]["cell"] == {"sheet": "BOM", "row": 1, "column": 2}
    assert sum(f.get("classification_status") == "unclassified" for f in survey["files"]) == 1


def test_unusable_reply_retries_the_whole_batch():
    def mutate(reply, count):
        if count == 1:
            reply["version"] = "private-response-value"
        return reply
    view, agent, survey = run(mutate=mutate)
    classify, feedback = retry_feedback(agent)
    assert classify == list(view.ids) and feedback == {
        "previous_reply_rejected": "SURVEY_REPLY_INVALID",
        "instruction": "Return a complete corrected reply for every id in classify."}
    assert not survey["has_blocking_findings"] and survey["model_calls"] == 2


def test_choice_job_log_line_carries_only_codes_and_ids():
    from choice_job import _summary
    pdf, _, survey = refused_run("ASSY-100 drawing.pdf", path("private-response-value"))
    line = _summary({"request_id": "r1", "status": "completed", "result": {"survey": survey}})
    assert line == {"request_id": "r1", "status": "completed", "survey_failures": [{
        "code": "SURVEY_FILES_UNCLASSIFIED", "severity": "warning", "failure_code": "SURVEY_EVIDENCE_NOT_IN_PATH",
        "file_ids": [pdf], "attempts": [{"failure_code": "SURVEY_EVIDENCE_NOT_IN_PATH", "file_id": pdf, "field": "assembly"}] * 2}]}
    assert "private-response-value" not in json.dumps(line) and "package.zip" not in json.dumps(line)
    assert _summary({"request_id": "r2", "status": "failed", "code": "PACKAGE_FILE_UNAVAILABLE"}) == {
        "request_id": "r2", "status": "failed", "code": "PACKAGE_FILE_UNAVAILABLE"}
    assert _summary({"request_id": "r3", "status": "completed", "result": {"survey": {"findings": []}}}) == {
        "request_id": "r3", "status": "completed"}


def test_file_paths_are_shown_as_written_except_key_like_runs():
    key = "AbCdEf0123456789AbCdEf0123456789XyZ0"
    roots = [("RFQ-PACKAGE-TWO-ASSY.zip", archive({
        "ENE010-0810-U2-TK/3-27-2026_assy_YonduV10p1_014-0810-U2-U3.pdf": b"%PDF-1.4 synthetic",
        f"keys/{key}.txt": b"plain text"}))]
    view = build_view(expand_package(roots))
    shown = {entry["path"] for entry in view.entries.values()}
    # The A034 drawing name was hidden by the shared token rule; names must stay citable.
    assert "RFQ-PACKAGE-TWO-ASSY.zip/ENE010-0810-U2-TK/3-27-2026_assy_YonduV10p1_014-0810-U2-U3.pdf" in shown
    assert "RFQ-PACKAGE-TWO-ASSY.zip/keys/[REDACTED].txt" in shown
    assert not any(key in path for path in shown)
    folders = {item["folder"] for item in view.package["folders"]}
    assert "RFQ-PACKAGE-TWO-ASSY.zip/ENE010-0810-U2-TK" in folders


def test_a_path_citation_of_a_long_file_name_is_accepted():
    name = "ENE010-0810-U2-TK/3-27-2026_assy_YonduV10p1_014-0810-U2-U3.pdf"
    roots = [("RFQ-PACKAGE-TWO-ASSY.zip", archive({name: b"%PDF-1.4 synthetic"}))]

    def agent(_instructions, user_text):
        batch, _ = json.JSONDecoder().raw_decode(user_text)
        cited = batch["files"][0]["path"].rsplit("/", 1)[-1]
        return {"version": "package-survey-reply.v1", "files": [record(
            batch["classify"][0], "other", assembly={"name": [path("ENE010-0810-U2-TK")], "revision": []},
            evidence=[path(cited)])], "findings": []}
    result = survey_package(roots, agent)
    assert result["model_calls"] == 1 and not result["has_blocking_findings"]
    assert not any(f.get("classification_status") == "unclassified" for f in result["files"])


# --- QQ #182: a call with no reply does not use up a classification attempt -----------------------

class ServiceDown(Exception):
    """Stands in for the model client's wrapper (ChatClientException) around the HTTP error."""


class HttpError(Exception):
    status_code = 503


def no_reply():
    exc = ServiceDown("private transport detail")
    exc.__cause__ = HttpError("private transport detail")
    return exc


NO_REPLY_ATTEMPT = {"failure_code": "SURVEY_CALL_FAILED", "file_id": None, "exception_type": "ServiceDown",
                    "cause_type": "HttpError", "status_code": 503}


def scripted_run(script, *, max_calls=None, roots=None):
    """script[n] decides call n (from 1): an exception is raised, 'refuse' cites a changed cell for the
    ASSY-200 BOM, anything else answers correctly. Returns the BOM id, requests sent, waits and survey."""
    roots = roots or a030_like()
    view = build_view(expand_package(roots))
    target = by_name(view).get("ASSY-200 BOM.xlsx")
    agent = StubAgent(a030_answers(view)) if target else None
    sent, waits = [], []

    def call(instructions, user_text):
        sent.append(user_text)
        step = script(len(sent)) if callable(script) else script.get(len(sent))
        if isinstance(step, BaseException):
            raise step
        reply = agent(instructions, user_text)
        if step == "refuse":
            for item in reply["files"]:
                if item["file_id"] == target:
                    item["evidence"] = [cell("BOM", 1, 2, "private-response-value")]
        return reply
    survey = survey_package(roots, call, max_calls=max_calls, sleep=waits.append)
    return target, sent, waits, survey


def batch_failure(survey, code="SURVEY_BATCH_FAILED"):
    return next(f for f in survey["findings"] if f["code"] == code)


def test_a_call_with_no_reply_is_sent_again_without_using_an_attempt():
    _, sent, waits, survey = scripted_run({1: no_reply()})
    assert survey["model_calls"] == len(sent) == 2 and waits == [2.0]
    # The model never saw the first request, so the same request is sent again with no feedback.
    assert sent[1] == sent[0] and "previous_reply_rejected" not in sent[1]
    assert not survey["has_blocking_findings"]
    assert not any(f.get("classification_status") == "unclassified" for f in survey["files"])


def test_two_calls_with_no_reply_then_a_reply_complete_within_the_cap():
    _, sent, waits, survey = scripted_run({1: no_reply(), 2: no_reply()})
    assert survey["model_calls"] == len(sent) == 3 and waits == [2.0, 8.0]
    assert not survey["has_blocking_findings"] and len(survey["files"]) == 8


def test_calls_with_no_reply_past_the_transport_retries_fail_with_the_existing_codes():
    _, sent, waits, survey = scripted_run(lambda _: no_reply())
    # Two transport retries, then the batch's two attempts; nothing further is sent.
    assert survey["model_calls"] == len(sent) == 4 and waits == [2.0, 8.0, 8.0]
    assert all(text == sent[0] for text in sent)
    finding = batch_failure(survey)
    assert finding["severity"] == "blocking" and finding["failure_code"] == "SURVEY_CALL_FAILED"
    # Each failed call is recorded with class names and the HTTP status only, never the message.
    assert finding["attempt_failures"] == [NO_REPLY_ATTEMPT] * 4
    assert "private transport detail" not in json.dumps(survey)


def test_a_refused_citation_still_gets_exactly_one_retry_around_a_call_with_no_reply():
    bom, sent, waits, survey = scripted_run({1: "refuse", 2: no_reply(), 3: "refuse"})
    assert survey["model_calls"] == len(sent) == 3 and waits == [2.0]
    # The retry after the lost call still carries the refusal; only the refused file is asked again.
    assert sent[2] == sent[1] and '"failure_code":"SURVEY_EVIDENCE_VALUE_CHANGED"' in sent[1]
    assert json.JSONDecoder().raw_decode(sent[1])[0]["classify"] == [bom]
    finding = batch_failure(survey)
    assert finding["file_ids"] == [bom] and finding["failure_code"] == "SURVEY_EVIDENCE_VALUE_CHANGED"
    refusal = {"failure_code": "SURVEY_EVIDENCE_VALUE_CHANGED", "file_id": bom, "field": "evidence"}
    assert finding["attempt_failures"] == [refusal, NO_REPLY_ATTEMPT, refusal]
    assert "private-response-value" not in sent[1]


def test_a_refused_citation_corrected_after_a_call_with_no_reply_completes():
    _, sent, _, survey = scripted_run({1: "refuse", 2: no_reply()})
    assert survey["model_calls"] == 3 and not survey["has_blocking_findings"]


def test_two_refused_replies_still_fail_without_waiting():
    bom, sent, waits, survey = scripted_run({1: "refuse", 2: "refuse"})
    assert survey["model_calls"] == len(sent) == 2 and waits == []
    assert batch_failure(survey)["failure_code"] == "SURVEY_EVIDENCE_VALUE_CHANGED"


def test_a_reply_that_cannot_be_read_still_uses_an_attempt():
    _, sent, waits, survey = scripted_run(lambda _: SurveyError("SURVEY_REPLY_TRUNCATED"))
    assert survey["model_calls"] == len(sent) == 2 and waits == []
    finding = batch_failure(survey)
    assert finding["failure_code"] == "SURVEY_REPLY_TRUNCATED"
    assert finding["attempt_failures"] == [{"failure_code": "SURVEY_REPLY_TRUNCATED", "file_id": None}] * 2


def test_the_call_ceiling_counts_calls_with_no_reply():
    _, sent, waits, survey = scripted_run(lambda _: no_reply(), max_calls=2)
    # No wait is spent before a call the ceiling will not allow.
    assert survey["model_calls"] == len(sent) == 2 and waits == [2.0]
    finding = batch_failure(survey, "SURVEY_CALL_LIMIT")
    assert finding["severity"] == "blocking" and finding["attempt_failures"] == [NO_REPLY_ATTEMPT] * 2


def test_the_default_ceiling_gives_each_batch_its_own_transport_retries():
    rows = [["Header", "Value"]] + [[f"row {index}", "x" * 150] for index in range(1, 20)]
    members = {f"folder{index // 10}/sheet{index}.xlsx": xlsx({"S": rows}) for index in range(30)}
    roots = [("big.zip", archive(members))]
    batches = plan_batches(build_view(expand_package(roots)))
    assert len(batches) > 1
    _, sent, _, survey = scripted_run(lambda _: no_reply(), roots=roots)
    assert survey["model_calls"] == len(sent) == 4 * len(batches)
    assert {f["code"] for f in survey["findings"] if f["raised_by"] == "code"} == {"SURVEY_BATCH_FAILED"}
