"""How a CSV's bytes are decoded, shared by the survey, the profile, the author view and row accounting.

A UTF-16 LE or BE byte-order mark selects UTF-16 (Excel's "Unicode text" export and other tools
write CSVs this way); anything else is read as UTF-8, with 'utf-8-sig' dropping a UTF-8 mark. A
UTF-32 mark, which begins like the UTF-16 LE one, is not taken for UTF-16, and a file with no mark
is never guessed to be UTF-16: such files fail UTF-8 decoding loudly where they did before.
Standard library only. Run python -m pytest tests/test_text_encoding.py.
"""
from __future__ import annotations

import codecs


def csv_encoding(data: bytes) -> str:
    """'utf-16' for a UTF-16 LE or BE byte-order mark, else 'utf-8-sig'."""
    if data.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        return "utf-8-sig"
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return "utf-16"
    return "utf-8-sig"
