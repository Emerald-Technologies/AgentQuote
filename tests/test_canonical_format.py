"""Variable manufacturer/MPN pair format (plan 8.12, Nick 2026-10-08): synthetic data only."""
import io

import openpyxl
import pandas as pd
import pytest

from bom_processor.canonical_format import (CANONICAL_COLUMNS, MAX_MANUFACTURER_PAIRS, PairCountError,
                                            canonical_columns, canonical_pair_count, pair_indexes,
                                            row_pair_count)
from bom_processor.core import ProcessingError
from bom_processor.remote import _validate_xlsx_bytes
from bom_processor.package_resolution import PackageResolutionError, ParentPackageService
from bom_processor.semantic import AssemblyIdentity, reconcile_boms


def row(index=1, pairs=8, item="R1"):
    value = {name: None for name in canonical_columns(max(8, pairs))}
    value.update(line_number=index, item_number=item, item_name="Resistor", item_category="Part", quantity=2)
    for pair in range(1, pairs + 1):
        value[f"manufacturer_{pair}"], value[f"manufacturer_item_number_{pair}"] = f"Maker {pair}", f"MPN-{pair}"
    return value


def workbook(rows, pairs):
    stream = io.BytesIO()
    pd.DataFrame(rows, columns=canonical_columns(pairs)).to_excel(stream, index=False)
    return stream.getvalue()


def test_eight_pairs_is_exactly_the_classic_25_columns():
    assert len(CANONICAL_COLUMNS) == 25 and canonical_columns() == CANONICAL_COLUMNS
    assert canonical_pair_count(CANONICAL_COLUMNS) == 8
    assert len(canonical_columns(20)) == 49 and canonical_pair_count(canonical_columns(20)) == 20
    assert len(canonical_columns(MAX_MANUFACTURER_PAIRS)) == 9 + 2 * MAX_MANUFACTURER_PAIRS


@pytest.mark.parametrize("columns", [
    CANONICAL_COLUMNS[:-1],                                    # half a pair
    CANONICAL_COLUMNS[:-2],                                    # fewer than 8 pairs
    CANONICAL_COLUMNS + ["manufacturer_10", "manufacturer_item_number_10"],  # gap at 9
    CANONICAL_COLUMNS + ["manufacturer_item_number_9", "manufacturer_9"],    # swapped order
    canonical_columns(MAX_MANUFACTURER_PAIRS) + ["manufacturer_51", "manufacturer_item_number_51"],
])
def test_non_canonical_headers_are_refused(columns):
    assert canonical_pair_count(columns) is None


def test_row_pair_count_follows_the_widest_line_and_refuses_above_the_maximum():
    assert row_pair_count([row(pairs=3)]) == 8
    assert row_pair_count([row(pairs=3), row(2, pairs=20)]) == 20
    with pytest.raises(PairCountError, match="MANUFACTURER_PAIR_LIMIT"):
        row_pair_count([{"manufacturer_51": "x"}])
    with pytest.raises(PairCountError):
        canonical_columns(7)
    assert list(pair_indexes({"manufacturer_12": None})) == list(range(1, 13))


def test_output_validation_accepts_a_20_pair_workbook_and_checks_every_pair():
    _validate_xlsx_bytes(workbook([row(pairs=20)], 20), 100)
    _validate_xlsx_bytes(workbook([row(pairs=3)], 8), 100)
    broken = row(pairs=20)
    broken["manufacturer_item_number_17"] = None
    with pytest.raises(ProcessingError, match="manufacturer pair invalid"):
        _validate_xlsx_bytes(workbook([broken], 20), 100)
    stream = io.BytesIO()
    pd.DataFrame([row(pairs=9)], columns=canonical_columns(9)).drop(columns=["manufacturer_item_number_9"]).to_excel(stream, index=False)
    with pytest.raises(ProcessingError, match="parsed schema invalid"):
        _validate_xlsx_bytes(stream.getvalue(), 100)


def test_reconciliation_and_resolved_workbook_keep_every_pair():
    left, right = [row(pairs=12)], [row(pairs=12)]
    right[0]["manufacturer_13"], right[0]["manufacturer_item_number_13"] = "Maker 13", "MPN-13"
    result = reconcile_boms([left, right], assembly=AssemblyIdentity("Board", "A"))
    assert result.relation == "merged"
    data = ParentPackageService._workbook(result.rows)
    header = [cell.value for cell in openpyxl.load_workbook(io.BytesIO(data)).active[1]]
    assert canonical_pair_count(header) == 13
    frame = pd.read_excel(io.BytesIO(data))
    assert {frame.loc[0, f"manufacturer_item_number_{index}"] for index in range(1, 14)} == {f"mpn-{i}" for i in range(1, 14)}
    # An 8-pair result is written in exactly the classic columns.
    narrow = ParentPackageService._workbook([row(pairs=2)])
    assert [cell.value for cell in openpyxl.load_workbook(io.BytesIO(narrow)).active[1]] == CANONICAL_COLUMNS


def test_more_pairs_than_the_maximum_is_a_finding_never_a_truncation():
    wide = {**row(pairs=8), **{f"manufacturer_{i}": f"M{i}" for i in range(9, 52)},
            **{f"manufacturer_item_number_{i}": f"P{i}" for i in range(9, 52)}}
    result = reconcile_boms([[wide]], assembly=AssemblyIdentity("Board", "A"))
    assert result.relation == "conflict" and result.evidence[0]["kind"] == "manufacturer_mpn_limit"
    with pytest.raises(PackageResolutionError, match="MANUFACTURER_PAIR_LIMIT"):
        ParentPackageService._workbook([wide])
