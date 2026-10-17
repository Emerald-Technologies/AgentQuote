"""CSV byte-order marks (QQ #99; synthetic test kit, QuickQuote PR #232). Offline, synthetic files.

A CSV saved as UTF-16 with a byte-order mark is read the same as its UTF-8 copy everywhere a BOM
CSV is decoded: the profile, the author's preview, row accounting and the review counts.
"""
import codecs

import pytest

from bom_processor.text_encoding import csv_encoding

TEXT = "mystery,name,qty,maker,maker part\r\nP1,Widget,2,Acme,A-100\r\nP2,Gadget µF,1,Acme,A-200\r\n"
UTF8 = TEXT.encode("utf-8")
UTF16_LE = codecs.BOM_UTF16_LE + TEXT.encode("utf-16-le")
UTF16_BE = codecs.BOM_UTF16_BE + TEXT.encode("utf-16-be")


@pytest.mark.parametrize("data, encoding", [
    (UTF8, "utf-8-sig"), (codecs.BOM_UTF8 + UTF8, "utf-8-sig"), (UTF16_LE, "utf-16"), (UTF16_BE, "utf-16"),
    (TEXT.encode("utf-16-le"), "utf-8-sig"), (TEXT.encode("utf-32"), "utf-8-sig"), (b"", "utf-8-sig"),
], ids=["utf-8", "utf-8 mark", "utf-16 le", "utf-16 be", "utf-16 no mark", "utf-32", "empty"])
def test_csv_encoding_follows_the_byte_order_mark_only(data, encoding):
    assert csv_encoding(data) == encoding


@pytest.mark.parametrize("data", [UTF16_LE, UTF16_BE, codecs.BOM_UTF8 + UTF8], ids=["utf-16 le", "utf-16 be", "utf-8 mark"])
def test_a_marked_csv_is_read_like_its_utf8_copy(data):
    from bom_processor.candidate_review import _source_counts
    from bom_processor.foundry_author import build_preview
    from bom_processor.master_registry import profile_bytes
    from bom_processor.row_accounting import source_inventory
    assert source_inventory(data, ".csv").cells == source_inventory(UTF8, ".csv").cells
    marked, plain = profile_bytes(data, ".csv"), profile_bytes(UTF8, ".csv")
    assert (marked.sheet_names, marked.header_candidates, marked.layout) == (
        plain.sheet_names, plain.header_candidates, plain.layout)
    assert build_preview(data, "csv") == build_preview(UTF8, "csv")
    assert _source_counts(data, ".csv") == _source_counts(UTF8, ".csv")
