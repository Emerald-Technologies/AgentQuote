"""Combined BOM split (QuickQuote A045): each requested assembly gets only its own lines."""
import pytest

from bom_processor.bom_split import SplitError, covers_once, split_rows

REQUESTED = [{"assembly_id": "a1", "part_number": "0100-04931", "revision": "02"},
             {"assembly_id": "a2", "part_number": "0100-04932", "revision": "02"}]


def a045():
    # Output rows 2..13 as A045's sheet: each board's own line, then its children (Parent Part column).
    numbers = {2: "0100-04931", 3: "0120-05191", 4: "0130-04488", 5: "0320-04550", 6: "0110-04455", 7: "0630-03236",
               8: "0100-04932", 9: "0120-05192", 10: "0130-04489", 11: "0320-04551", 12: "0110-04456", 13: "0630-03323"}
    parents = {2: None, 3: 2, 4: 2, 5: 2, 6: 2, 7: 2, 8: None, 9: 8, 10: 8, 11: 8, 12: 8, 13: 8}
    return numbers, parents


def test_a045_splits_into_each_boards_own_lines():
    numbers, parents = a045()
    subsets = split_rows(numbers, parents, REQUESTED)
    assert [(s["assembly_id"], s["head_row"], s["output_rows"]) for s in subsets] == [
        ("a1", 2, [2, 3, 4, 5, 6, 7]), ("a2", 8, [8, 9, 10, 11, 12, 13])]
    assert all(s["line_count"] == 12 for s in subsets)
    assert covers_once(subsets)


def test_single_assembly_and_sub_assembly_boms_are_not_split():
    numbers, parents = a045()
    # One board with the other as a child line (a sub-assembly): one requested head, no split.
    nested = {**parents, 8: 2}
    assert split_rows(numbers, nested, REQUESTED) is None
    # Flat BOM without its own assembly line: nothing heads it.
    flat = {row: None for row in numbers if row not in (2, 8)}
    assert split_rows({row: numbers[row] for row in flat}, flat, REQUESTED) is None
    # Unproven structure (missing parents) is never split.
    assert split_rows(numbers, {2: None}, REQUESTED) is None


@pytest.mark.parametrize("change, code", [
    (lambda n, p: p.update({14: None}) or n.update({14: "9999-0001"}), "SPLIT_ROW_UNASSIGNED"),
    (lambda n, p: n.update({8: "0100-04931"}), "SPLIT_ASSEMBLY_REPEATED"),
    (lambda n, p: p.update({3: 4, 4: 3}), "SPLIT_STRUCTURE_INVALID"),
])
def test_unsafe_combined_boms_go_to_a_person(change, code):
    numbers, parents = a045()
    change(numbers, parents)
    with pytest.raises(SplitError, match=code):
        split_rows(numbers, parents, REQUESTED)


def test_covers_once_refuses_overlap_gaps_and_one_assembly_twice():
    numbers, parents = a045()
    good = split_rows(numbers, parents, REQUESTED)
    assert not covers_once(good[:1])
    assert not covers_once([good[0], {**good[1], "output_rows": good[1]["output_rows"][:-1]}])
    assert not covers_once([good[0], {**good[1], "output_rows": good[1]["output_rows"] + [7]}])
    assert not covers_once([good[0], {**good[1], "assembly_id": "a1"}])


def test_split_documents_accepts_only_proven_subsets_that_cover_a_file_once():
    from bom_processor.bom_split import split_documents
    numbers, parents = a045()
    one, two = split_rows(numbers, parents, REQUESTED)
    group = lambda subset: {"document_ids": ["doc"], "evidence": {"fixtures": [{"document_ids": ["doc"], "subset": subset}]}}
    assert split_documents([group(one), group(two)]) == {"doc"}
    assert split_documents([{"document_ids": ["other"], "evidence": {"fixtures": []}}]) == set()
    # The same file in two groups without subsets is still refused (one document, one result).
    plain = {"document_ids": ["doc"], "evidence": {"fixtures": [{"document_ids": ["doc"]}]}}
    assert split_documents([plain, plain]) is None
    assert split_documents([group(one), group(one)]) is None
