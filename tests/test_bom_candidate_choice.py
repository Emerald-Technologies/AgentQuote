"""Stage 2 primary-BOM choice (plan 8.3): candidate view, overlap counts, checks. Synthetic, offline."""
import copy
import json

import pytest

from bom_processor.bom_candidate_choice import (CHOICE_INSTRUCTIONS, REPLY_VERSION, ChoiceError,
                                                compare_reads, post_parse_check, run_choice,
                                                verify_choice)
from bom_processor.bom_candidate_view import (BOTTOM_ROWS, CALL_CHAR_LIMIT, CandidateViewError,
                                              build_candidate_view)
from bom_processor.bom_primary_selection import SELECTION_GUIDANCE
from bom_processor.package_survey import build_view, expand_package
from test_package_survey import archive, xlsx

HEADER = ["Item", "Part Number", "Manufacturer", "MPN", "Qty"]


def lines(count, *, start=1, qty=1):
    return [[index, f"P-{index}", "Maker", f"M-{index}", qty] for index in range(start, start + count)]


def package(files):
    return build_view(expand_package([("package.zip", archive(files))]))


def ids_by_name(view):
    return {item.chain[-1].rsplit("/", 1)[-1]: file_id for file_id, item in view.files.items()}


def shown(view, file_id, value):
    """A cell citation for a value the candidate view showed (found, never assumed)."""
    for (sheet, row, column), actual in view.shown[file_id].items():
        if str(actual) == value:
            return {"source": "cell", "value": value, "cell": {"sheet": sheet, "row": row, "column": column}}
    raise AssertionError("value not shown")


def columns(first_row=2, last_row=None, identity=2, quantity=5):
    return {"sheet": "BOM", "identity_column": identity, "quantity_column": quantity,
            "first_row": first_row, "last_row": last_row}


def reply(view, primary, others, *, cols=None, findings=()):
    cols = cols or {}
    files = []
    for file_id in view.file_ids:
        disposition = "primary" if file_id == primary else others[file_id]
        files.append({"file_id": file_id, "disposition": disposition, "rationale": "Synthetic reason.",
                      "evidence": [shown(view, file_id, "Part Number")],
                      "columns": cols.get(file_id, columns())})
    return {"version": REPLY_VERSION, "primary": primary, "files": files,
            "rationale": "Synthetic choice.", "findings": list(findings)}


def two_boms(mod_rows, plain_rows, names=("ASSY-100 MOD BOM.xlsx", "ASSY-100 BOM.xlsx")):
    survey = package({names[0]: xlsx({"BOM": [HEADER] + mod_rows}),
                      names[1]: xlsx({"BOM": [HEADER] + plain_rows})})
    ids = ids_by_name(survey)
    view = build_candidate_view(survey, "ASSY-100", [ids[names[0]], ids[names[1]]])
    return view, ids[names[0]], ids[names[1]]


def test_view_shows_top_bottom_and_low_header_rows_and_records_shown_cells():
    title = [["Title block"]] + [[f"note {index}"] for index in range(30)]
    rows = title + [HEADER] + lines(200)
    survey = package({"a.xlsx": xlsx({"BOM": rows}), "b.xlsx": xlsx({"BOM": [HEADER] + lines(5)})})
    ids = ids_by_name(survey)
    view = build_candidate_view(survey, "ASSY-100", [ids["a.xlsx"], ids["b.xlsx"]])
    sheet = view.entries[ids["a.xlsx"]]["sheets"][0]
    shown_rows = [row for row, _ in sheet["shown_rows"]]
    assert sheet["rows"] == len(rows) and sheet["header_row_guess"] == 32
    assert shown_rows[:20] == list(range(1, 21))                     # top
    assert 32 in shown_rows and 52 in shown_rows                      # header plus 20
    assert shown_rows[-BOTTOM_ROWS:] == list(range(len(rows) - BOTTOM_ROWS + 1, len(rows) + 1))
    assert 100 not in shown_rows                                     # the middle is not sent
    assert all(key[1] in shown_rows for key in view.shown[ids["a.xlsx"]])
    # Whole-file reads are kept for code checks, not sent.
    assert len({row for _, row, _ in view.cells[ids["a.xlsx"]]}) == len(rows)
    assert len(view.user_text) <= CALL_CHAR_LIMIT


def test_overlap_counts_cover_every_row_and_never_carry_values():
    view, mod, plain = two_boms(lines(176), lines(150))
    # The shared header cell counts as a value too: 150 lines + 1 header.
    by_columns = {(item["left"]["column"], item["right"]["column"]): item for item in view.overlaps}
    for column in (2, 4):
        item = by_columns[(column, column)]
        distinct = {item["left"]["file"]: item["left"]["distinct"], item["right"]["file"]: item["right"]["distinct"]}
        assert item["shared"] == 151 and distinct == {mod: 177, plain: 151}
    text = json.dumps(view.overlaps)
    assert "P-1" not in text and "M-1" not in text


def test_superset_choice_agrees_without_a_person():
    view, mod, plain = two_boms(lines(6), lines(5))
    result = verify_choice(view, reply(view, mod, {plain: "redundant"}))
    assert result["agreement"] == "agreed" and result["automatic_selection_allowed"] is True
    assert result["cross_checks"][0]["comparison"]["relation"] == "superset"
    assert [item["disposition"] for item in result["files"]] == ["primary", "redundant"] or \
        {item["file_id"]: item["disposition"] for item in result["files"]} == {mod: "primary", plain: "redundant"}


def test_cross_check_reads_rows_outside_the_view():
    # The extra line sits in the middle of a long BOM, where the view shows nothing.
    plain = lines(120)
    mod = lines(60) + [[999, "P-EXTRA", "Maker", "M-EXTRA", 1]] + lines(60, start=61)
    view, mod_id, plain_id = two_boms(plain, mod, names=("p.xlsx", "m.xlsx"))
    # The agent picks the file lacking the extra line and calls the other redundant: wrong.
    result = verify_choice(view, reply(view, mod_id, {plain_id: "redundant"}))
    assert result["agreement"] == "needs_review" and result["review_reasons"] == ["disagrees:redundant"]
    assert result["cross_checks"][0]["comparison"]["right_only_values"] == 1


def test_quantity_conflict_goes_to_review():
    view, mod, plain = two_boms(lines(5, qty=2), lines(5, qty=1))
    result = verify_choice(view, reply(view, mod, {plain: "redundant"}))
    assert result["review_reasons"] == ["conflict:redundant"]


def test_obsolete_by_folder_is_recorded_and_left_for_review():
    survey = package({"current/ASSY-100 BOM.xlsx": xlsx({"BOM": [HEADER] + lines(5)}),
                      "obsolete/ASSY-100 BOM.xlsx": xlsx({"BOM": [HEADER] + lines(4)})})
    ids = {item.path.split("/")[-2]: file_id for file_id, item in survey.files.items()}
    view = build_candidate_view(survey, "ASSY-100", [ids["current"], ids["obsolete"]])
    answer = reply(view, ids["current"], {ids["obsolete"]: "obsolete"})
    answer["files"][1]["evidence"] = [{"source": "path", "value": "obsolete"}]
    result = verify_choice(view, answer)
    assert result["review_reasons"] == ["not_confirmable:obsolete"]
    assert any(item["code"] == "CHOICE_FROM_NAME_ONLY" and item["file_id"] == ids["obsolete"]
               for item in result["findings"])


def test_name_that_disagrees_with_contents_is_kept_as_an_agent_finding():
    view, mod, plain = two_boms(lines(6), lines(5), names=("ASSY-100 MOD BOM.xlsx", "ASSY-999 BOM.xlsx"))
    finding = {"code": "NAME_CONTENT_DISAGREE", "severity": "warning",
               "rationale": "The name gives another assembly; the contents match this one."}
    result = verify_choice(view, reply(view, mod, {plain: "redundant"}, findings=[finding]))
    assert result["agreement"] == "agreed"
    assert result["findings"][0]["code"] == "NAME_CONTENT_DISAGREE" and result["findings"][0]["raised_by"] == "agent"


@pytest.mark.parametrize("mutate, code", [
    (lambda r: r.update(primary="F9"), "CHOICE_PRIMARY_INVALID"),
    (lambda r: r["files"].pop(), "CHOICE_COVERAGE_INVALID"),
    (lambda r: r["files"][0]["evidence"][0].update(value="Invented"), "CHOICE_EVIDENCE_VALUE_CHANGED"),
    (lambda r: r["files"][0]["evidence"][0]["cell"].update(row=100), "CHOICE_EVIDENCE_CELL_NOT_SHOWN"),
    (lambda r: r["files"][0].update(evidence=[{"source": "overlap", "id": "O99"}]), "CHOICE_EVIDENCE_OVERLAP_UNKNOWN"),
    (lambda r: r["files"][1].update(columns=None), "CHOICE_COLUMNS_REQUIRED"),
    (lambda r: r["files"][0]["columns"].update(identity_column=40), "CHOICE_COLUMNS_EMPTY"),
    (lambda r: r["files"][0]["columns"].update(sheet="Nope"), "CHOICE_COLUMNS_SHEET_UNKNOWN"),
])
def test_invalid_replies_are_refused_with_a_stable_code(mutate, code):
    view, mod, plain = two_boms(lines(6), lines(5))
    answer = reply(view, mod, {plain: "redundant"})
    mutate(answer)
    with pytest.raises(ChoiceError, match=code):
        verify_choice(view, answer)


def test_overlap_citation_is_accepted():
    view, mod, plain = two_boms(lines(6), lines(5))
    answer = reply(view, mod, {plain: "redundant"})
    answer["files"][1]["evidence"] = [{"source": "overlap", "id": view.overlaps[0]["id"]}]
    assert verify_choice(view, answer)["agreement"] == "agreed"


def test_run_choice_retries_once_with_the_code_only_then_stops():
    view, mod, plain = two_boms(lines(6), lines(5))
    good = reply(view, mod, {plain: "redundant"})
    sent = []

    def call(instructions, text):
        sent.append(text)
        answer = copy.deepcopy(good)
        if len(sent) == 1:
            answer["primary"] = "F9"
        return answer

    result, calls = run_choice(view, call)
    assert calls == 2 and result["agreement"] == "agreed"
    assert sent[1].endswith('"previous_reply_rejected":"CHOICE_PRIMARY_INVALID","instruction":'
                            '"Return a complete corrected reply for every id in choose."}')
    with pytest.raises(ChoiceError, match="CHOICE_PRIMARY_INVALID"):
        run_choice(view, lambda i, t: {**copy.deepcopy(good), "primary": "F9"})


def test_post_parse_check_catches_a_redundant_file_holding_parts_the_primary_lacks():
    view, mod, plain = two_boms(lines(6), lines(5) + [[7, "P-ONLY-PLAIN", "Maker", "M-ONLY", 1]])
    choice = verify_choice(view, reply(view, mod, {plain: "redundant"}))
    # The column read already sees it; the parsed primary is checked independently too.
    parsed = [{"item_number": f"P-{index}", "manufacturer_item_number_1": f"M-{index}"} for index in range(1, 7)]
    result = post_parse_check(choice, view, parsed)
    assert result["agreement"] == "needs_review" and result["review_reasons"] == ["post_parse_disagrees:redundant"]
    assert result["checks"][0]["column_values_not_in_primary"] == 1


def test_post_parse_check_agrees_when_the_parsed_primary_contains_the_set_aside_file():
    view, mod, plain = two_boms(lines(6), lines(5))
    choice = verify_choice(view, reply(view, mod, {plain: "redundant"}))
    parsed = [{"item_number": f"P-{index}", "manufacturer_item_number_1": f"M-{index}"} for index in range(1, 7)]
    result = post_parse_check(choice, view, parsed)
    assert result["agreement"] == "agreed" and result["checks"][0]["parsed_field"] == "item_number"


def test_compare_reads_relations():
    a = [("p1", "1"), ("p2", "2")]
    assert compare_reads(a, a)["relation"] == "identical"
    assert compare_reads(a, a[:1])["relation"] == "superset"
    assert compare_reads(a[:1], a)["relation"] == "subset"
    assert compare_reads(a, [("p1", "1"), ("p3", "1")])["relation"] == "overlap"
    assert compare_reads(a, [("p1", "5")])["relation"] == "conflict"
    # Repeated identities are present but never paired, so they cannot create a conflict.
    assert compare_reads([("p1", "1"), ("p1", "2")], [("p1", "3")])["relation"] == "identical"


def test_view_needs_two_known_candidates_and_bounds_its_size():
    survey = package({"a.xlsx": xlsx({"BOM": [HEADER] + lines(3)}), "b.xlsx": xlsx({"BOM": [HEADER] + lines(3)})})
    ids = ids_by_name(survey)
    with pytest.raises(CandidateViewError, match="CANDIDATE_FILES_INVALID"):
        build_candidate_view(survey, "A", [ids["a.xlsx"]])
    with pytest.raises(CandidateViewError, match="CANDIDATE_FILES_INVALID"):
        build_candidate_view(survey, "A", [ids["a.xlsx"], "F99"])
    wide = [[f"long text value {row}-{column} " * 3 for column in range(20)] for row in range(500)]
    files = {f"{index}.xlsx": xlsx({"BOM": [HEADER] + wide}) for index in range(5)}
    survey = package(files)
    view = build_candidate_view(survey, "A", list(survey.ids))
    assert len(view.user_text) <= CALL_CHAR_LIMIT
    assert all(entry.get("view_limited") for entry in view.entries.values())


def test_instructions_carry_the_selection_guidance_and_the_reply_version():
    assert SELECTION_GUIDANCE in CHOICE_INSTRUCTIONS and REPLY_VERSION in CHOICE_INSTRUCTIONS
