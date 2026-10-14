"""Skim view for large BOMs (plan 8.8 step 4c). Synthetic workbooks only."""
import io
import json

import pytest
from openpyxl import Workbook

from bom_processor.foundry_author import FULL_SOURCE_CHARS, build_source_contexts
from bom_processor.skim_view import (BOTTOM_ROWS, MAX_CHARS, MAX_UNUSUAL_ROWS, MIDDLE_ROWS, TOP_ROWS,
                                     build_skim_view)


def bom(lines, columns=20, alternates_every=0, text_qty_at=()):
    book = Workbook(); sheet = book.active; sheet.title = "BOM"
    sheet.append(["Customer BOM 4711 Rev C"])
    sheet.append(["Find", "Part", "Description", "Qty"] + [f"Attr{i}" for i in range(1, columns - 3)])
    for n in range(1, lines + 1):
        qty = "two" if n in text_qty_at else n % 7 + 1
        sheet.append([n, f"P-{n:05d}", f"Part number {n} description text", qty]
                     + [f"v{n}-{i}" for i in range(1, columns - 3)])
        if alternates_every and n % alternates_every == 0:
            sheet.append([None, None, None, None, f"ALT-{n}"])
    sheet.append(["Total", None, None, lines])
    data = io.BytesIO(); book.save(data); return data.getvalue()


def size(view):
    return len(json.dumps(view, ensure_ascii=False, separators=(",", ":")))


@pytest.mark.parametrize("lines", [50, 500, 5000])
def test_view_size_is_bounded_whatever_the_number_of_lines(lines):
    view = build_skim_view(bom(lines, columns=12 if lines == 5000 else 20), ".xlsx")
    assert view["coverage"] == "skim" and size(view) <= MAX_CHARS
    sheet = view["sheets"][0]
    assert sheet["row_count"] == lines + 3
    assert len(sheet["top"]) == min(TOP_ROWS, lines + 3)


def test_top_and_bottom_are_kept_and_the_total_row_is_visible():
    view = build_skim_view(bom(500), ".xlsx")
    sheet = view["sheets"][0]
    assert [row for row, _ in sheet["top"]] == list(range(1, TOP_ROWS + 1))
    assert [row for row, _ in sheet["bottom"]][-1] == sheet["last_row"]
    assert len(sheet["bottom"]) == BOTTOM_ROWS
    assert sheet["bottom"][-1][1][0] == [1, "Total"]
    assert len(sheet["middle_sample"]) <= MIDDLE_ROWS


def test_unusual_rows_are_flagged_with_the_row_above():
    view = build_skim_view(bom(500, alternates_every=50, text_qty_at=(300,)), ".xlsx")
    unusual = view["sheets"][0]["unusual_rows"]
    reasons = {item["reason"] for item in unusual}
    assert "text_in_number_column" in reasons and "blank_in_main_column" in reasons
    alternate = next(item for item in unusual if item["reason"] == "blank_in_main_column")
    assert alternate["row_above"][0] == alternate["row"] - 1
    assert len(unusual) <= MAX_UNUSUAL_ROWS


def test_every_column_has_a_profile():
    view = build_skim_view(bom(500), ".xlsx")
    columns = view["sheets"][0]["columns"]
    assert [item["column"] for item in columns] == list(range(1, 21))
    qty = columns[3]
    assert qty["filled"] == 502 and qty["numeric"] >= 500  # header and title are text


def test_very_wide_sheets_trim_the_middle_first_never_top_or_bottom():
    view = build_skim_view(bom(1800, columns=48), ".xlsx")  # within the inventory's 100,000-cell bound
    sheet = view["sheets"][0]
    assert size(view) <= MAX_CHARS
    assert len(sheet["top"]) == TOP_ROWS and len(sheet["bottom"]) == BOTTOM_ROWS
    assert view["trimmed"]["middle_sample"] > 0


def test_small_packages_are_still_sent_in_full():
    contexts = build_source_contexts([{"bytes": bom(50), "sha256": "a" * 64, "extension": "xlsx"}])
    assert contexts[0]["inventory"]["coverage"] == "complete"


def test_a_large_source_is_skimmed_and_small_neighbours_stay_complete():
    small, large = bom(30, columns=6), bom(2000)
    contexts = build_source_contexts([{"bytes": small, "sha256": "a" * 64, "extension": "xlsx"},
                                      {"bytes": large, "sha256": "b" * 64, "extension": "xlsx"}])
    assert [item["inventory"]["coverage"] for item in contexts] == ["complete", "skim"]
    assert sum(len(json.dumps(item)) for item in contexts) <= FULL_SOURCE_CHARS
