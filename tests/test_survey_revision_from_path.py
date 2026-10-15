"""A survey revision cited as a whole file name (QQ A057, Nick 2026-10-14).

The survey agent cited each BOM's file name as its revision ('BOM-0100-04201-REV-02.xlsx'); its digit
runs never agree with the requested '02', so every link was unconfirmed and the package choice found
no requested BOM. A cited text that also names the part number now gives only the token after its
REV marker, and with no marker the revision is unknown, never a contradiction. Synthetic values.
Run: python -m pytest tests/test_survey_revision_from_path.py
"""
import pytest

from bom_processor.package_survey_check import _confirm, cited_revision

REQUESTED = {"assembly_id": "aaaaaaaa-1111-4111-8111-111111111111", "part_number": "0100-04201", "revision": "02"}
NAME = "BOM-0100-04201-REV-02.xlsx"


def link(revision_text, requested=REQUESTED):
    assembly = {"name": [{"source": "path", "value": NAME}],
                "revision": [{"source": "path", "value": revision_text}]}
    return _confirm(requested, "R1", "0100-04201", [{"source": "path", "value": NAME}], assembly)


def test_a_file_name_revision_that_agrees_confirms_the_link():
    assert cited_revision("0100-04201", NAME) == "02"
    result = link(NAME)
    assert (result["link"], result["unconfirmed_reason"]) == ("confirmed", None)


def test_a_file_name_revision_that_differs_still_contradicts():
    result = link(NAME, {**REQUESTED, "revision": "03"})
    assert (result["link"], result["unconfirmed_reason"]) == ("unconfirmed", "revision_differs")


@pytest.mark.parametrize("text", ["BOM 0100-04201.xlsx", "package/0100-04201/parts list.xlsx"])
def test_a_path_without_a_revision_marker_is_unknown_not_a_contradiction(text):
    assert cited_revision("0100-04201", text) is None
    assert link(text)["link"] == "confirmed"


def test_a_plain_cited_revision_is_unchanged():
    assert cited_revision("0100-04201", "02") == "02"
    assert link("02")["link"] == "confirmed"
    result = link("03")
    assert (result["link"], result["unconfirmed_reason"]) == ("unconfirmed", "revision_differs")


@pytest.mark.parametrize("text, revision", [
    ("0100-04201 Rev 03 build list", "03"), ("0100-04201_revision_B.xlsx", "B"), ("PREVIEW 0100-04201", None),
])
def test_revision_markers_are_read_as_written(text, revision):
    assert cited_revision("0100-04201", text) == revision
