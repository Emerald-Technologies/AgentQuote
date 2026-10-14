"""Manufacturer/MPN layouts (plan 8.12, step 4d): 20 pairs per line in every layout seen, read with
no pair lost; a broken pattern is a finding. Synthetic data only."""
import copy
import io
import json

import pytest
from openpyxl import Workbook

from bom_processor.canonical_format import canonical_columns
from bom_processor.row_accounting import VERSION, verify_row_map

PAIRS = 20
LINES = 3


def xlsx(rows, title="BOM"):
    book = Workbook(); sheet = book.active; sheet.title = title
    for row in rows:
        sheet.append(row)
    data = io.BytesIO(); book.save(data); return data.getvalue()


def maker(line, pair):
    return f"Maker {line}-{pair}"


def mpn(line, pair):
    return f"PN-{line:02d}-{pair:02d}"


def layout(kind):
    """Source rows and, per line, [(manufacturer link, mpn link)] for 20 pairs. Columns 1-3: part, desc, qty."""
    header = ["Part", "Description", "Qty"]
    if kind == "repeated_unnumbered":
        header += [h for _ in range(PAIRS) for h in ("Manufacturer", "MPN")]
        cols = [(4 + 2 * i, 5 + 2 * i) for i in range(PAIRS)]
    elif kind == "numbered_mpn_first":
        header += [h for i in range(1, PAIRS + 1) for h in (f"MPN{i}", f"MFG{i}")]
        cols = [(5 + 2 * i, 4 + 2 * i) for i in range(PAIRS)]
    elif kind == "numbered_mfg_first":
        header += [h for i in range(1, PAIRS + 1) for h in (f"MFG{i}", f"MPN{i}")]
        cols = [(4 + 2 * i, 5 + 2 * i) for i in range(PAIRS)]
    else:  # combined: one manufacturer column and one MPN column, separated values in pair order
        header += ["Manufacturers", "MPNs"]
        cols = None
    rows = [header]
    links = []
    for line in range(1, LINES + 1):
        row = [f"P-{line}", f"Widget {line}", line]
        if cols is None:
            row += ["; ".join(maker(line, p) for p in range(1, PAIRS + 1)),
                    "; ".join(mpn(line, p) for p in range(1, PAIRS + 1))]
            links.append([([line + 1, 4, "part"], [line + 1, 5, "part"]) for _ in range(PAIRS)])
        else:
            values = [None] * (3 + 2 * PAIRS)
            for p, (m_col, n_col) in enumerate(cols, 1):
                values[m_col - 4] = maker(line, p); values[n_col - 4] = mpn(line, p)
            row += values[: 2 * PAIRS]
            links.append([([line + 1, m_col], [line + 1, n_col]) for m_col, n_col in cols])
        rows.append(row)
    return rows, links


def build(kind):
    rows, links = layout(kind)
    columns = canonical_columns(PAIRS)
    out_rows = [columns]
    lines = []
    for line in range(1, LINES + 1):
        values = dict.fromkeys(columns)
        values.update(line_number=line, item_number=f"P-{line}", item_name=f"Widget {line}",
                      item_category="Part", quantity=line)
        fields = {"item_number": [line + 1, 1], "item_name": [line + 1, 2], "quantity": [line + 1, 3]}
        for p, (m_link, n_link) in enumerate(links[line - 1], 1):
            values[f"manufacturer_{p}"] = maker(line, p); values[f"manufacturer_item_number_{p}"] = mpn(line, p)
            fields[f"manufacturer_{p}"] = list(m_link); fields[f"manufacturer_item_number_{p}"] = list(n_link)
        out_rows.append([values[c] for c in columns])
        lines.append({"output_row": line + 1, "sheet": "BOM", "rows": [line + 1], "fields": fields})
    row_map = {"version": VERSION, "lines": lines,
               "skipped": [{"sheet": "BOM", "rows": [[1, 1]], "reason": "header", "rationale": "Headings."}],
               "ignored_columns": []}
    return xlsx(rows), xlsx(out_rows, title="Parsed"), row_map, out_rows


def check(source, output, row_map):
    return verify_row_map(source, ".xlsx", output, json.dumps(row_map).encode())


LAYOUTS = ["repeated_unnumbered", "numbered_mpn_first", "numbered_mfg_first", "combined"]


@pytest.mark.parametrize("kind", LAYOUTS)
def test_twenty_pairs_in_every_layout_are_read_with_no_pair_lost(kind):
    source, output, row_map, _ = build(kind)
    result = check(source, output, row_map)
    assert result["valid"] is True, (result["failed_rows"], result["pattern_findings"], result["unmapped_columns"])
    assert result["output_lines"] == LINES and result["pattern_findings"] == []


@pytest.mark.parametrize("kind", ["repeated_unnumbered", "numbered_mpn_first", "numbered_mfg_first"])
def test_a_dropped_pair_is_found_on_its_row(kind):
    source, _, row_map, out_rows = build(kind)
    # The parser stops at 19 pairs on line 2: output and row map both omit pair 20.
    columns = out_rows[0]
    out_rows = copy.deepcopy(out_rows)
    out_rows[2][columns.index("manufacturer_20")] = None
    out_rows[2][columns.index("manufacturer_item_number_20")] = None
    del row_map["lines"][1]["fields"]["manufacturer_20"]; del row_map["lines"][1]["fields"]["manufacturer_item_number_20"]
    result = check(source, xlsx(out_rows, title="Parsed"), row_map)
    assert {"sheet": "BOM", "row": 3, "code": "PAIR_NOT_CARRIED"} in result["failed_rows"]


def test_names_and_mpns_swapped_in_some_pairs_is_a_finding():
    source, _, row_map, out_rows = build("numbered_mfg_first")
    columns = out_rows[0]
    out_rows = copy.deepcopy(out_rows)
    # Line 1, pair 5: maker and MPN written the wrong way round, cited consistently with the swap.
    m_col, n_col = row_map["lines"][0]["fields"]["manufacturer_5"][1], row_map["lines"][0]["fields"]["manufacturer_item_number_5"][1]
    out_rows[1][columns.index("manufacturer_5")], out_rows[1][columns.index("manufacturer_item_number_5")] = mpn(1, 5), maker(1, 5)
    row_map["lines"][0]["fields"]["manufacturer_5"] = [2, n_col]
    row_map["lines"][0]["fields"]["manufacturer_item_number_5"] = [2, m_col]
    result = check(source, xlsx(out_rows, title="Parsed"), row_map)
    assert result["valid"] is False
    assert {(item["column"], item["code"]) for item in result["pattern_findings"]} >= {
        (m_col, "MANUFACTURER_MPN_SWAPPED"), (n_col, "MANUFACTURER_MPN_SWAPPED")}


def test_a_manufacturer_column_paired_with_different_mpn_columns_is_a_finding():
    # Line 2 pairs maker 1 with MPN 2 (and maker 2 with MPN 1); the values are cited correctly,
    # so only the cross-line pattern check can see that the pairing is wrong.
    source, _, row_map, out_rows = build("repeated_unnumbered")
    columns = out_rows[0]
    out_rows = copy.deepcopy(out_rows)
    line = out_rows[2]
    a, b = columns.index("manufacturer_item_number_1"), columns.index("manufacturer_item_number_2")
    line[a], line[b] = line[b], line[a]
    fields = row_map["lines"][1]["fields"]
    fields["manufacturer_item_number_1"], fields["manufacturer_item_number_2"] = (
        fields["manufacturer_item_number_2"], fields["manufacturer_item_number_1"])
    m1, m2 = fields["manufacturer_1"][1], fields["manufacturer_2"][1]
    result = check(source, xlsx(out_rows, title="Parsed"), row_map)
    assert result["valid"] is False and result["failed_rows"] == []
    assert {(item["column"], item["code"]) for item in result["pattern_findings"]} == {
        (m1, "PAIR_COLUMNS_INCONSISTENT"), (m2, "PAIR_COLUMNS_INCONSISTENT")}


def test_an_ignored_column_holding_makers_is_a_finding():
    # Real BOMs reuse makers across pairs; the parser reads 19 pairs and declares pair 20 ignored.
    rows, _ = layout("repeated_unnumbered")
    for line in range(1, LINES + 1):
        rows[line][3 + 2 * (PAIRS - 1)] = maker(line, 1)  # pair 20's maker repeats pair 1's
    source = xlsx(rows)
    _, _, row_map, out_rows = build("repeated_unnumbered")
    columns = out_rows[0]
    out_rows = copy.deepcopy(out_rows)
    for line in range(1, LINES + 1):
        out_rows[line][columns.index("manufacturer_20")] = None
        out_rows[line][columns.index("manufacturer_item_number_20")] = None
        m_col = row_map["lines"][line - 1]["fields"].pop("manufacturer_20")[1]
        n_col = row_map["lines"][line - 1]["fields"].pop("manufacturer_item_number_20")[1]
    row_map["ignored_columns"] = [{"sheet": "BOM", "column": m_col, "reason": "Extra column."},
                                  {"sheet": "BOM", "column": n_col, "reason": "Extra column."}]
    result = check(source, xlsx(out_rows, title="Parsed"), row_map)
    assert result["valid"] is False and result["unmapped_columns"] == []
    assert {"sheet": "BOM", "column": m_col, "code": "PAIR_COLUMN_IGNORED"} in result["pattern_findings"]


def test_a_combined_cell_with_unequal_name_and_mpn_counts_is_a_finding():
    rows, _ = layout("combined")
    rows[1][4] = rows[1][4].rsplit("; ", 1)[0]  # line 1 MPN cell has 19 parts, names have 20
    source = xlsx(rows)
    _, output, row_map, out_rows = build("combined")
    columns = out_rows[0]
    out_rows = copy.deepcopy(out_rows)
    out_rows[1][columns.index("manufacturer_item_number_20")] = mpn(1, 19)  # cite an existing part
    row_map["lines"][0]["fields"]["manufacturer_item_number_20"] = [2, 5, "part"]
    result = check(source, xlsx(out_rows, title="Parsed"), row_map)
    assert {"sheet": "BOM", "row": 2, "code": "COMBINED_PAIR_COUNT_MISMATCH"} in result["failed_rows"]


def test_a_combined_cell_part_left_out_is_a_finding():
    source, _, row_map, out_rows = build("combined")
    columns = out_rows[0]
    out_rows = copy.deepcopy(out_rows)
    out_rows[1][columns.index("manufacturer_20")] = None
    out_rows[1][columns.index("manufacturer_item_number_20")] = None
    del row_map["lines"][0]["fields"]["manufacturer_20"]; del row_map["lines"][0]["fields"]["manufacturer_item_number_20"]
    result = check(source, xlsx(out_rows, title="Parsed"), row_map)
    assert {"sheet": "BOM", "row": 2, "code": "COMBINED_PART_NOT_CARRIED"} in result["failed_rows"]


def test_reading_guidance_covers_every_layout():
    from bom_processor.document_role import BOM_READING_GUIDANCE
    text = BOM_READING_GUIDANCE.lower()
    for phrase in ("repeated", "numbered", "either order", "combined", "separator", "swapped"):
        assert phrase in text
