"""Core-number rule (QuickQuote round 2, D1/D3). Offline; A035 package names."""
import pytest

from bom_processor.assembly_core import core_groups, find_assembly, revision_contradicts

ENE = "ENE010-0810-U2-TK"
SNC = "SNC006-002022-01-TK"


def test_core_strips_glued_customer_prefix_and_letters_only_suffix():
    assert core_groups(ENE) == ("010", "0810", "U2")
    assert core_groups(SNC) == ("006", "002022", "01")
    assert core_groups("010-0810-U2") == ("010", "0810", "U2")


@pytest.mark.parametrize("number", ["AB-12", "X12-TK", "PCA 1234"])
def test_short_numbers_are_never_over_trimmed(number):
    # Fewer than two groups or five digits after trimming: the full number is the core.
    assert core_groups(number) == tuple(number.replace("-", " ").split())


@pytest.mark.parametrize("text, expected", [
    ("RFQ-PACKAGE-TWO-ASSY.zip/ENE010-0810-U2 v10p1/BOM.xlsx", "ENE010-0810-U2"),
    ("ENE010-0810-U2-TK", "ENE010-0810-U2-TK"),
    ("010_0810_U2 bom.xlsx", "010_0810_U2"),
    ("010 0810 U2", "010 0810 U2"),
    ("ene010-0810-u2_BOM.xlsx", "ene010-0810-u2"),
    ("ENE010-0810-U2-A", "ENE010-0810-U2"),
])
def test_text_naming_the_core_is_found_as_written(text, expected):
    assert find_assembly(ENE, text) == expected


@pytest.mark.parametrize("text", [
    "ENE011-0810-00.zip",                       # a different first group: a different assembly
    "Energous Yondu V10 013-0810-00p1",         # the bare board, related but not the assembly
    "ENE010-0810-U2-03 BOM.xlsx",               # a sibling dash number
    "ENE010-0810-U23",                          # glued to more characters
    "9010-0810-U2",                             # glued digits are not a customer prefix
    "0810-U2",                                  # part of the core only
])
def test_other_numbers_are_not_the_assembly(text):
    assert find_assembly(ENE, text) is None


def test_snc_file_name_and_full_number():
    assert find_assembly(SNC, "SNC006-002022-01-TK.xls") == "SNC006-002022-01-TK"
    assert find_assembly(SNC, "Folder/006-002022-01 Rev 13 BOM.xlsx") == "006-002022-01"
    assert find_assembly(SNC, "006-002022-02 BOM.xlsx") is None
    assert find_assembly(SNC, "ENE010-0810-U2 v10p1") is None


def test_invalid_inputs_find_nothing():
    assert find_assembly("", "anything") is None
    assert find_assembly(ENE, None) is None


@pytest.mark.parametrize("requested, found, contradicts", [
    ("V10p1-B", "v10p1", False),
    ("V10p1-B", "V10.1", False),
    ("V10p1-B", "V10", False),          # one extends the other
    ("V10p1-B", "v9", True),
    ("13", "13", False),
    ("13", "Rev 12", True),
    ("A", "Rev A", False),
    ("A", "B", True),
    ("V10p1-B", "B", False),            # mixed forms never contradict
    ("13", None, False),
    (None, "13", False),
    ("", "", False),
])
def test_revision_contradiction(requested, found, contradicts):
    assert revision_contradicts(requested, found) is contradicts
