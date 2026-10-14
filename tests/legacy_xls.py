"""Write a minimal legacy binary Excel workbook (OLE2 compound file holding BIFF8 records) for tests.

A046 (Nick 2026-10-14): customer BOMs arrive as legacy .xls, and no writer for that format is
installed (xlrd only reads it). This writes just enough for xlrd: one General cell format, text
cells as LABEL records and numbers as NUMBER records, one Workbook stream kept above the 4096-byte
mini-stream cutoff so it lives in ordinary sectors. Synthetic test data only.
Usage: legacy_xls({"Sheet": [[cell, ...], ...]}) -> bytes; None leaves a cell empty.
"""
import struct

_SECTOR = 512
_END, _FREE, _FAT = 0xFFFFFFFE, 0xFFFFFFFF, 0xFFFFFFFD


def _record(opcode, data=b""):
    return struct.pack("<HH", opcode, len(data)) + data


def _text(value, length_bytes):
    encoded = value.encode("utf-16-le")
    return struct.pack("<" + ("H" if length_bytes == 2 else "B"), len(value)) + b"\x01" + encoded


def _sheet_stream(rows):
    width = max((len(row) for row in rows), default=0)
    body = _record(0x0809, struct.pack("<HHHHII", 0x0600, 0x0010, 0x0DBB, 0x07CC, 0, 6))
    body += _record(0x0200, struct.pack("<IIHHH", 0, len(rows), 0, width, 0))
    for row_index, row in enumerate(rows):
        for column, value in enumerate(row):
            if value is None:
                continue
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                body += _record(0x0203, struct.pack("<HHHd", row_index, column, 0, float(value)))
            else:
                body += _record(0x0204, struct.pack("<HHH", row_index, column, 0) + _text(str(value), 2))
    return body + _record(0x000A)


def _workbook_stream(sheets):
    names = list(sheets)
    head = _record(0x0809, struct.pack("<HHHHII", 0x0600, 0x0005, 0x0DBB, 0x07CC, 0, 6))
    head += _record(0x0042, struct.pack("<H", 1200))
    head += _record(0x00E0, bytes(20))  # one cell XF using format 0 (General)
    bound_sizes = [len(_record(0x0085, b"\0" * (6 + len(_text(name, 1))))) for name in names]
    offset = len(head) + sum(bound_sizes) + len(_record(0x000A))
    bound, bodies = b"", b""
    for name in names:
        body = _sheet_stream(sheets[name])
        bound += _record(0x0085, struct.pack("<IBB", offset, 0, 0) + _text(name, 1))
        bodies += body
        offset += len(body)
    return head + bound + _record(0x000A) + bodies


def _entry(name, kind, child, start, size):
    encoded = (name.encode("utf-16-le") + b"\0\0") if name else b""
    return (encoded.ljust(64, b"\0") + struct.pack("<HBB", len(encoded), kind, 1 if kind else 0)
            + struct.pack("<III", _FREE, _FREE, child) + bytes(16) + bytes(4) + bytes(16)
            + struct.pack("<IIi", start, size, 0))


def legacy_xls(sheets):
    stream = _workbook_stream(sheets)
    stream = stream.ljust(max(4096, -(-len(stream) // _SECTOR) * _SECTOR), b"\0")
    data_sectors = len(stream) // _SECTOR
    if data_sectors + 2 > _SECTOR // 4:
        raise ValueError("test workbook too large for one FAT sector")
    directory_sector = 1 + data_sectors
    fat = [_FAT] + [index + 2 for index in range(data_sectors - 1)] + [_END, _END]
    fat += [_FREE] * (_SECTOR // 4 - len(fat))
    header = (bytes.fromhex("d0cf11e0a1b11ae1") + bytes(16)
              + struct.pack("<HHHHH", 0x003E, 0x0003, 0xFFFE, 9, 6) + bytes(6)
              + struct.pack("<IIIIIIIII", 0, 1, directory_sector, 0, 4096, _END, 0, _END, 0)
              + struct.pack("<I", 0) + struct.pack("<I", _FREE) * 108)
    directory = (_entry("Root Entry", 5, 1, _END, 0) + _entry("Workbook", 2, _FREE, 1, len(stream))
                 + _entry("", 0, _FREE, 0, 0) * 2)
    return header + struct.pack("<128I", *fat) + stream + directory
