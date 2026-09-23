"""Local-only approved parser registry and candidate approval evidence.

Candidate bytes are quarantined evidence and are never imported or executed.
The filesystem store is intentionally single-process; production requires Blob
conditional writes and an approved promotion service.
"""
from __future__ import annotations

import csv
import hashlib
from html.parser import HTMLParser
import io
import json
import os
import posixpath
import re
import threading
import uuid
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence
from xml.etree import ElementTree

MAX_PROFILE_BYTES = 32 * 1024 * 1024
MAX_CANDIDATE_BYTES = 8 * 1024 * 1024
MAX_ZIP_ENTRIES = 256
MAX_ZIP_ENTRY_BYTES = 8 * 1024 * 1024
MAX_ZIP_TOTAL_BYTES = 32 * 1024 * 1024
MAX_HEADER_ROWS = 8
MAX_HEADER_CELLS = 128
MAX_XLS_SHEETS = 32
MAX_XLS_ROWS = 8
MAX_HTML_BYTES = 8 * 1024 * 1024
MAX_HTML_ROWS = 10_000
MAX_HTML_CELLS_PER_ROW = 256
MAX_HTML_CELLS = 250_000
MAX_HTML_DEPTH = 32
MAX_HTML_CELL_TEXT = 32 * 1024
MAX_HTML_TEXT = 8 * 1024 * 1024
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SAFE_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")
_SHA = re.compile(r"^[0-9a-f]{64}$")


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _hash(value: Any) -> str:
    return hashlib.sha256(value if isinstance(value, bytes) else _json(value).encode()).hexdigest()


def _valid_id(value: str, code: str = "INVALID_ID") -> str:
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
        raise RegistryError(code)
    return value


def _valid_sha(value: str, code: str = "INVALID_HASH") -> str:
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise RegistryError(code)
    return value


def normalize_header(value: Any) -> str:
    text = str(value or "").replace("\ufeff", "").strip().lower()
    return re.sub(r"[^a-z0-9]+", "_", text).strip("_")[:256]


class RegistryError(Exception):
    """Stable public error with bounded non-sensitive details."""
    def __init__(self, code: str, details: Mapping[str, Any] | None = None):
        self.code = code
        allowed = {"reason", "parser_key", "profile_hash", "match_count"}
        self.details = {key: value for key, value in dict(details or {}).items() if key in allowed}
        super().__init__(code)


@dataclass(frozen=True)
class StructuralProfile:
    extension: str
    size_bytes: int
    sha256: str
    sheet_names: tuple[str, ...]
    header_candidates: Mapping[str, tuple[tuple[str, ...], ...]]
    source_subtype: str = ""
    profile_hash: str = field(init=False)

    def __post_init__(self) -> None:
        subtype = self.source_subtype or _subtype_for_extension(self.extension)
        object.__setattr__(self, "source_subtype", subtype)
        # Candidate identity is structural: content-specific size and digest
        # remain evidence, but must not prevent equivalent sources deduping.
        structural = self.to_dict(False)
        structural.pop("size_bytes", None)
        structural.pop("sha256", None)
        object.__setattr__(self, "profile_hash", _hash(structural))

    def to_dict(self, include_hash: bool = True) -> dict[str, Any]:
        value = {"extension": self.extension, "size_bytes": self.size_bytes, "sha256": self.sha256,
                 "sheet_names": list(self.sheet_names), "header_candidates": {name: [list(row) for row in rows] for name, rows in sorted(self.header_candidates.items())}}
        if self.source_subtype != _subtype_for_extension(self.extension):
            value["source_subtype"] = self.source_subtype
        if include_hash: value["profile_hash"] = self.profile_hash
        return value


def _csv_profile(data: bytes):
    rows = []
    for row in csv.reader(io.StringIO(data.decode("utf-8-sig", errors="replace"))):
        rows.append(tuple(normalize_header(cell) for cell in row[:MAX_HEADER_CELLS]))
        if len(rows) == MAX_HEADER_ROWS: break
    return ("Sheet1",), {"Sheet1": tuple(rows)}


def _subtype_for_extension(extension: str) -> str:
    return {".xls": "ole_xls", ".xlsx": "xlsx", ".csv": "csv"}.get(extension.lower(), "unknown")


class _HTMLTableProfiler(HTMLParser):
    """Small inert HTML table reader; it never interprets markup or URLs."""
    _allowed = {"html", "head", "meta", "title", "body", "table", "thead", "tbody", "tfoot", "tr", "th", "td", "span", "strong", "b", "em", "i", "br"}
    _void = {"meta", "br"}
    _cell = {"th", "td"}
    _attrs = {"http-equiv", "content", "style", "valign", "id", "border", "cellpadding", "cellspacing", "align"}
    _numeric = re.compile(r"^(?:0|[1-9][0-9]{0,3})$")
    _enum = {"valign": {"top", "middle", "bottom", "baseline"}, "align": {"left", "center", "right", "justify"}}
    _style_properties = {"color", "background", "background-color", "font-family", "font-size", "font-weight", "text-align", "vertical-align", "white-space"}

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.stack = []
        self.rows = []
        self.current_row = None
        self.current_cell = None
        self.cell_text = []
        self.total_text = 0
        self.tables = 0

    def _check_name(self, tag):
        if tag not in self._allowed: raise ValueError("tag")
        if len(self.stack) >= MAX_HTML_DEPTH: raise ValueError("depth")

    def _check_parent(self, tag):
        parent = self.stack[-1] if self.stack else None
        allowed = {
            None: {"html"}, "html": {"head", "body"}, "head": {"meta", "title"},
            "body": {"table", "span", "br"}, "table": {"thead", "tbody", "tfoot", "tr"},
            "thead": {"tr"}, "tbody": {"tr"}, "tfoot": {"tr"}, "tr": self._cell,
            "th": {"span", "strong", "b", "em", "i", "br"}, "td": {"span", "strong", "b", "em", "i", "br"},
            "span": {"span", "strong", "b", "em", "i", "br"},
            "strong": {"span", "b", "em", "i", "br"}, "b": {"span", "strong", "em", "i", "br"},
            "em": {"span", "strong", "b", "i", "br"}, "i": {"span", "strong", "b", "em", "br"},
        }
        if tag not in allowed.get(parent, set()): raise ValueError("nesting")

    @classmethod
    def _style(cls, value):
        if len(value) > 2048 or "url" in value.lower() or "@" in value or "(" in value or ";" not in value and not value.strip():
            raise ValueError("style")
        for declaration in value.split(";"):
            if not declaration.strip(): continue
            if ":" not in declaration: raise ValueError("style")
            name, raw = (part.strip().lower() for part in declaration.split(":", 1))
            if name not in cls._style_properties or not raw or len(raw) > 256 or any(ord(c) < 32 for c in raw):
                raise ValueError("style")
            if any(c in raw for c in "{}[]<>") or re.search(r"(?:expression|javascript|data)\s*:", raw, re.I):
                raise ValueError("style")

    @classmethod
    def _attrs_for(cls, tag, attrs):
        seen = set()
        for name, value in attrs:
            name = name.lower()
            if name in seen or name not in cls._attrs: raise ValueError("attribute")
            seen.add(name); value = value or ""
            if name == "style": cls._style(value)
            elif name == "id" and not _SAFE_ID.fullmatch(value): raise ValueError("id")
            elif name in cls._enum and value.lower() not in cls._enum[name]: raise ValueError("enum")
            elif name in {"border", "cellpadding", "cellspacing"} and not cls._numeric.fullmatch(value): raise ValueError("number")
            elif name == "http-equiv" and (tag != "meta" or value.lower() not in {"content-type", "content-style-type"}): raise ValueError("meta")
            elif name == "content" and tag != "meta": raise ValueError("meta")
        permitted = {
            "meta": {"http-equiv", "content"}, "body": {"style", "valign"}, "tr": {"style", "valign"},
            "span": {"id", "style"}, "table": {"border", "cellpadding", "cellspacing"}, "th": {"align", "valign"},
        }
        if any(name not in permitted.get(tag, set()) for name in seen): raise ValueError("attribute")

    def handle_starttag(self, tag, attrs):
        tag = tag.lower(); self._check_name(tag)
        self._check_parent(tag)
        if tag == "table":
            self.tables += 1
            if self.tables > 1: raise ValueError("tables")
        self._attrs_for(tag, attrs)
        if tag == "tr":
            if self.current_row is not None: raise ValueError("nesting")
            self.current_row = []
        elif tag in self._cell:
            if self.current_row is None or self.current_cell is not None: raise ValueError("cell")
            if len(self.current_row) >= MAX_HTML_CELLS_PER_ROW: raise ValueError("cells")
            self.current_cell = tag; self.cell_text = []
        if tag not in self._void: self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag.lower() not in self._void: self.handle_endtag(tag)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if not self.stack or self.stack[-1] != tag: raise ValueError("nesting")
        self.stack.pop()
        if tag in self._cell:
            text = "".join(self.cell_text).strip()
            if len(text) > MAX_HTML_CELL_TEXT: raise ValueError("cell_text")
            self.current_row.append(normalize_header(text)); self.current_cell = None; self.cell_text = []
        elif tag == "tr":
            if self.current_row is None: raise ValueError("row")
            self.rows.append(tuple(self.current_row)); self.current_row = None
            if len(self.rows) > MAX_HTML_ROWS or sum(map(len, self.rows)) > MAX_HTML_CELLS: raise ValueError("rows")

    def handle_data(self, data):
        if any(ord(ch) < 32 and ch not in "\t\n\r" for ch in data): raise ValueError("control")
        self.total_text += len(data)
        if self.total_text > MAX_HTML_TEXT: raise ValueError("text")
        if self.current_cell is not None:
            self.cell_text.append(data)
            if sum(map(len, self.cell_text)) > MAX_HTML_CELL_TEXT: raise ValueError("cell_text")

    def _entity(self, value):
        import html
        decoded = html.unescape(value)
        if decoded == value or any(ord(ch) < 32 and ch not in "\t\n\r" for ch in decoded): raise ValueError("entity")
        self.handle_data(decoded)

    def handle_entityref(self, name):
        if name not in {"amp", "lt", "gt", "quot", "apos", "nbsp"}: raise ValueError("entity")
        self._entity("&" + name + ";")

    def handle_charref(self, name):
        try:
            number = int(name[1:], 16) if name.lower().startswith("x") else int(name, 10)
            if not 0 <= number <= 0x10ffff or 0xD800 <= number <= 0xDFFF: raise ValueError
            self._entity("&#" + name + ";")
        except ValueError: raise ValueError("entity") from None
    def handle_comment(self, data): raise ValueError("comment")
    def handle_decl(self, decl):
        if decl.strip().lower() != "doctype html": raise ValueError("declaration")
    def handle_pi(self, data): raise ValueError("processing_instruction")


def _html_profile(data: bytes):
    if len(data) > MAX_HTML_BYTES: raise RegistryError("PARSER_FORMAT_NOT_RECOGNIZED", {"reason": "html_bounds"})
    try:
        text = data.decode("utf-8-sig")
        parser = _HTMLTableProfiler(); parser.feed(text); parser.close()
        if (parser.stack or parser.current_row is not None or parser.current_cell is not None
                or parser.tables != 1 or not parser.rows or not any(parser.rows)):
            raise ValueError("incomplete")
        return ("HTMLTable",), {"HTMLTable": tuple(parser.rows[:MAX_HEADER_ROWS])}
    except (UnicodeDecodeError, ValueError) as exc:
        raise RegistryError("PARSER_FORMAT_NOT_RECOGNIZED", {"reason": "invalid_html_xls"}) from exc


def html_table_rows(data: bytes) -> tuple[tuple[str, ...], ...]:
    """Return bounded, normalized rows from a supported HTML-table workbook."""
    if len(data) > MAX_HTML_BYTES:
        raise RegistryError("PARSER_FORMAT_NOT_RECOGNIZED", {"reason": "html_bounds"})
    try:
        parser = _HTMLTableProfiler()
        parser.feed(data.decode("utf-8-sig")); parser.close()
        if parser.stack or parser.current_row is not None or parser.current_cell is not None or parser.tables != 1 or not parser.rows:
            raise ValueError("incomplete")
        return tuple(parser.rows)
    except (UnicodeDecodeError, ValueError) as exc:
        raise RegistryError("PARSER_FORMAT_NOT_RECOGNIZED", {"reason": "invalid_html_xls"}) from exc


def _bounded_zip_read(archive: zipfile.ZipFile, name: str) -> bytes:
    try: info = archive.getinfo(name)
    except KeyError as exc: raise RegistryError("PARSER_FORMAT_NOT_RECOGNIZED", {"reason": "missing_xlsx_part"}) from exc
    if info.file_size > MAX_ZIP_ENTRY_BYTES: raise RegistryError("PARSER_FORMAT_NOT_RECOGNIZED", {"reason": "zip_bounds"})
    with archive.open(info) as stream:
        data = stream.read(MAX_ZIP_ENTRY_BYTES + 1)
    if len(data) > MAX_ZIP_ENTRY_BYTES: raise RegistryError("PARSER_FORMAT_NOT_RECOGNIZED", {"reason": "zip_bounds"})
    return data


def _column_index(reference: str) -> int:
    match = re.match(r"([A-Z]+)", reference.upper())
    if not match: return 0
    value = 0
    for character in match.group(1): value = value * 26 + ord(character) - 64
    return value - 1


def _xlsx_profile(data: bytes):
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = archive.infolist()
            names = set()
            if len(infos) > MAX_ZIP_ENTRIES or sum(item.file_size for item in infos) > MAX_ZIP_TOTAL_BYTES:
                raise RegistryError("PARSER_FORMAT_NOT_RECOGNIZED", {"reason": "zip_bounds"})
            for item in infos:
                normalized = posixpath.normpath(item.filename)
                if (item.filename in names or item.flag_bits & 1 or item.file_size > MAX_ZIP_ENTRY_BYTES
                        or item.file_size > item.compress_size * 100 + 1024 or normalized != item.filename
                        or normalized.startswith("../") or normalized.startswith("/") or "\\" in item.filename):
                    raise RegistryError("PARSER_FORMAT_NOT_RECOGNIZED", {"reason": "unsafe_zip"})
                names.add(item.filename)
            namespace = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main", "p": "http://schemas.openxmlformats.org/package/2006/relationships"}
            workbook = ElementTree.fromstring(_bounded_zip_read(archive, "xl/workbook.xml"))
            relationships = ElementTree.fromstring(_bounded_zip_read(archive, "xl/_rels/workbook.xml.rels"))
            targets = {}
            for relationship in relationships.findall("p:Relationship", namespace):
                target = relationship.attrib.get("Target", "")
                if relationship.attrib.get("TargetMode", "Internal") != "Internal" or target.startswith("\\"):
                    raise RegistryError("PARSER_FORMAT_NOT_RECOGNIZED", {"reason": "unsafe_relationship"})
                # OOXML writers may use either package-absolute `/xl/...` or
                # workbook-relative `worksheets/...` relationship targets.
                resolved = (posixpath.normpath(target.lstrip("/")) if target.startswith("/")
                            else posixpath.normpath(posixpath.join("xl", target)))
                if resolved.startswith("../") or not resolved.startswith("xl/"):
                    raise RegistryError("PARSER_FORMAT_NOT_RECOGNIZED", {"reason": "unsafe_relationship"})
                targets[relationship.attrib.get("Id", "")] = resolved
            shared = []
            if "xl/sharedStrings.xml" in names:
                root = ElementTree.fromstring(_bounded_zip_read(archive, "xl/sharedStrings.xml"))
                for item in root.findall("m:si", namespace)[:4096]:
                    shared.append("".join(text.text or "" for text in item.findall(".//m:t", namespace))[:1024])
            sheet_names, headers = [], {}
            relation_attribute = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
            for sheet in workbook.findall("m:sheets/m:sheet", namespace)[:32]:
                name = sheet.attrib.get("name", "Sheet")[:128]
                target = targets.get(sheet.attrib.get(relation_attribute, ""))
                if not target or target not in names: continue
                root = ElementTree.fromstring(_bounded_zip_read(archive, target))
                rows = []
                for row in root.findall(".//m:sheetData/m:row", namespace)[:MAX_HEADER_ROWS]:
                    values = [""] * MAX_HEADER_CELLS
                    for cell in row.findall("m:c", namespace):
                        index = _column_index(cell.attrib.get("r", ""))
                        if not 0 <= index < MAX_HEADER_CELLS: continue
                        kind = cell.attrib.get("t")
                        raw = cell.find("m:v", namespace)
                        value = raw.text if raw is not None else ""
                        if kind == "s" and value.isdigit() and int(value) < len(shared): value = shared[int(value)]
                        elif kind == "inlineStr": value = "".join(text.text or "" for text in cell.findall(".//m:t", namespace))
                        values[index] = normalize_header(value)
                    while values and not values[-1]: values.pop()
                    rows.append(tuple(values))
                sheet_names.append(name); headers[name] = tuple(rows)
            return tuple(sheet_names) or ("Sheet1",), headers or {"Sheet1": ()}
    except RegistryError: raise
    except (zipfile.BadZipFile, ElementTree.ParseError, ValueError) as exc:
        raise RegistryError("PARSER_FORMAT_NOT_RECOGNIZED", {"reason": "invalid_xlsx"}) from exc


def profile_bytes(data: bytes, extension: str) -> StructuralProfile:
    if not isinstance(data, bytes) or len(data) > MAX_PROFILE_BYTES: raise RegistryError("PARSER_FORMAT_NOT_RECOGNIZED", {"reason": "profile_size_limit"})
    normalized = extension.lower() if extension.startswith(".") else "." + extension.lower()
    if normalized == ".csv": sheets, headers = _csv_profile(data)
    elif normalized == ".xlsx": sheets, headers = _xlsx_profile(data)
    elif normalized == ".xls":
        if data[:4] == b"\xd0\xcf\x11\xe0":
            subtype = "ole_xls"
        elif data.lstrip(b" \t\r\n").startswith((b"<", b"\xef\xbb\xbf<", b"\xff\xfe<", b"\xfe\xff<")):
            sheets, headers = _html_profile(data)
            return StructuralProfile(normalized, len(data), _hash(data), sheets, headers, "html_table_xls")
        else:
            subtype = "ole_xls"
        try:
            import xlrd
            book = xlrd.open_workbook(file_contents=data, on_demand=True)
            sheets, headers = [], {}
            for sheet in book.sheets()[:MAX_XLS_SHEETS]:
                sheets.append(sheet.name[:128]); rows = []
                for row in range(min(sheet.nrows, MAX_XLS_ROWS)):
                    rows.append(tuple(normalize_header(sheet.cell_value(row, col)) for col in range(min(sheet.ncols, MAX_HEADER_CELLS))))
                headers[sheet.name[:128]] = tuple(rows)
            return StructuralProfile(normalized, len(data), _hash(data), tuple(sheets) or ("Sheet1",), headers or {"Sheet1": ()}, subtype)
        except RegistryError: raise
        except Exception as exc: raise RegistryError("PARSER_FORMAT_NOT_RECOGNIZED", {"reason": "invalid_xls"}) from exc
    else: raise RegistryError("PARSER_FORMAT_NOT_RECOGNIZED", {"reason": "unsupported_extension"})
    return StructuralProfile(normalized, len(data), _hash(data), sheets, headers)


@dataclass(frozen=True)
class VariantSignature:
    extensions: frozenset[str]
    required_sheet_names: frozenset[str] = frozenset()
    required_headers: Mapping[str, frozenset[str]] = field(default_factory=dict)
    minimum_header_overlap: float = 1.0
    source_subtype: str = ""
    rule_hash: str = field(init=False)
    signature_hash: str = field(init=False)

    def __post_init__(self):
        extensions = frozenset(value.lower() if value.startswith(".") else "." + value.lower() for value in self.extensions)
        headers = {name: frozenset(normalize_header(value) for value in values) for name, values in self.required_headers.items()}
        if not extensions or not 0 < self.minimum_header_overlap <= 1: raise RegistryError("INVALID_SIGNATURE")
        object.__setattr__(self, "extensions", extensions); object.__setattr__(self, "required_headers", headers)
        subtype = self.source_subtype or ("ole_xls" if ".xls" in extensions else _subtype_for_extension(next(iter(extensions))))
        object.__setattr__(self, "source_subtype", subtype)
        object.__setattr__(self, "rule_hash", _hash(self.to_dict(False)))
        object.__setattr__(self, "signature_hash", _hash({"signature": self.to_dict(False)}))

    def to_dict(self, include_hash=True):
        value = {"extensions": sorted(self.extensions), "required_sheet_names": sorted(self.required_sheet_names), "required_headers": {name: sorted(headers) for name, headers in sorted(self.required_headers.items())}, "minimum_header_overlap": self.minimum_header_overlap}
        default_subtype = "ole_xls" if ".xls" in self.extensions else _subtype_for_extension(next(iter(self.extensions)))
        if self.source_subtype != default_subtype:
            value["source_subtype"] = self.source_subtype
        if include_hash: value.update(rule_hash=self.rule_hash, signature_hash=self.signature_hash)
        return value

    @classmethod
    def from_profile(cls, profile: Mapping[str, Any]) -> "VariantSignature":
        """Build the approval rule from the persisted candidate profile.

        Candidate requests persist JSON lists, while runtime profiles use
        tuples.  Normalize both representations in one place so the rule
        signed during approval is the same rule used by registry selection.
        """
        headers = profile.get("header_candidates", {})
        if not isinstance(headers, Mapping):
            raise RegistryError("INVALID_SIGNATURE")
        required = {}
        for name, rows in headers.items():
            if not isinstance(rows, (list, tuple)):
                raise RegistryError("INVALID_SIGNATURE")
            first = rows[0] if rows else ()
            if not isinstance(first, (list, tuple)):
                raise RegistryError("INVALID_SIGNATURE")
            required[name] = frozenset(value for value in first if value)
        extension = profile.get("extension", ".xlsx")
        sheet_names = profile.get("sheet_names", [])
        if not isinstance(sheet_names, (list, tuple)):
            raise RegistryError("INVALID_SIGNATURE")
        subtype = profile.get("source_subtype") or ("ole_xls" if extension.lower() in (".xls", "xls") else _subtype_for_extension(extension if str(extension).startswith(".") else "." + str(extension)))
        return cls(frozenset([extension]), frozenset(sheet_names), required, 1.0, subtype)

    @classmethod
    def from_dict(cls, value):
        extensions = frozenset(value["extensions"])
        subtype = value.get("source_subtype") or ("ole_xls" if ".xls" in extensions else _subtype_for_extension(next(iter(extensions))))
        signature = cls(extensions, frozenset(value.get("required_sheet_names", [])), {name: frozenset(headers) for name, headers in value.get("required_headers", {}).items()}, float(value.get("minimum_header_overlap", 1.0)), subtype)
        if value.get("signature_hash") != signature.signature_hash or value.get("rule_hash") != signature.rule_hash: raise RegistryError("REGISTRY_CORRUPT")
        return signature

    def matches(self, profile):
        if profile.extension not in self.extensions or profile.source_subtype != self.source_subtype or not self.required_sheet_names.issubset(profile.sheet_names): return False
        for name, required in self.required_headers.items():
            rows = profile.header_candidates.get(name, ())
            if not rows or max((len(required & set(row)) / len(required) if required else 1) for row in rows) < self.minimum_header_overlap: return False
        return True


@dataclass(frozen=True)
class FixtureBinding:
    fixture_id: str
    input_sha256: str
    expected_sha256: str
    actual_sha256: str
    status: str = "passed"
    def __post_init__(self):
        _valid_id(self.fixture_id, "INVALID_FIXTURE")
        for value in (self.input_sha256, self.expected_sha256, self.actual_sha256): _valid_sha(value, "INVALID_FIXTURE")
        if self.status != "passed": raise RegistryError("INVALID_FIXTURE")
    def to_dict(self): return asdict(self)


@dataclass(frozen=True)
class CandidateRecord:
    candidate_id: str; parser_key: str; customer: str; code_path: str; code_sha256: str
    signature_hash: str; rule_hash: str; fixture_bindings: tuple[FixtureBinding, ...]
    validation_status: str; created_identity: str; created_time: str
    def __post_init__(self):
        for value in (self.candidate_id, self.parser_key, self.customer, self.created_identity): _valid_id(value)
        for value in (self.code_sha256, self.signature_hash, self.rule_hash): _valid_sha(value)
        if self.validation_status != "passed" or not self.fixture_bindings: raise RegistryError("INVALID_CANDIDATE")
    def to_dict(self): return {**asdict(self), "fixture_bindings": [item.to_dict() for item in self.fixture_bindings]}
    @property
    def candidate_hash(self): return _hash(self.to_dict())
    @property
    def fixture_hash(self): return _hash([item.to_dict() for item in self.fixture_bindings])


@dataclass(frozen=True)
class ApprovalRecord:
    approval_id: str; candidate_id: str; parser_key: str; customer: str; variant_id: str; version: str
    candidate_hash: str; code_sha256: str; signature_hash: str; rule_hash: str; fixture_hash: str
    approver: str; approved_time: str; approval_hash: str = field(init=False)
    def __post_init__(self):
        for value in (self.approval_id, self.candidate_id, self.parser_key, self.customer, self.variant_id, self.approver): _valid_id(value)
        if not _SAFE_VERSION.fullmatch(self.version): raise RegistryError("INVALID_VERSION")
        for value in (self.candidate_hash, self.code_sha256, self.signature_hash, self.rule_hash, self.fixture_hash): _valid_sha(value)
        object.__setattr__(self, "approval_hash", _hash(self._bound_values()))
    def _bound_values(self):
        return {name: getattr(self, name) for name in (
            "approval_id", "candidate_id", "parser_key", "customer", "variant_id",
            "version", "candidate_hash", "code_sha256", "signature_hash", "rule_hash",
            "fixture_hash", "approver", "approved_time",
        )}
    def to_dict(self, include_hash=True):
        value = self._bound_values()
        if include_hash: value["approval_hash"] = self.approval_hash
        return value


@dataclass(frozen=True)
class ApprovedVariant:
    parser_key: str; variant_id: str; version: str; code_path: str; code_sha256: str
    signature_hash: str; rule_hash: str; approval_id: str
    def to_dict(self): return asdict(self)


class MasterRegistry:
    def __init__(self, variants: Sequence[ApprovedVariant] = (), signatures: Mapping[str, VariantSignature] | None = None):
        self.variants = list(variants); self.signatures = dict(signatures or {})
    def register_variant(self, variant, signature):
        if variant.signature_hash != signature.signature_hash or variant.rule_hash != signature.rule_hash: raise RegistryError("REGISTRY_CONFLICT")
        identity = (variant.parser_key, variant.variant_id, variant.version)
        for existing in self.variants:
            if (existing.parser_key, existing.variant_id, existing.version) == identity:
                if existing != variant: raise RegistryError("REGISTRY_CONFLICT")
                return
        self.variants.append(variant); self.signatures[signature.signature_hash] = signature
    def select(self, parser_key, data, extension):
        _valid_id(parser_key); profile = profile_bytes(data, extension)
        matches = [item for item in self.variants if item.parser_key == parser_key and self.signatures.get(item.signature_hash) and self.signatures[item.signature_hash].matches(profile)]
        if len(matches) == 1: return matches[0]
        code = "PARSER_FORMAT_AMBIGUOUS" if matches else "PARSER_FORMAT_NOT_RECOGNIZED"
        raise RegistryError(code, {"parser_key": parser_key, "profile_hash": profile.profile_hash, "match_count": len(matches)})


class LocalRegistryStore:
    def __init__(self, root):
        self.root = Path(root).resolve(); self._lock = threading.Lock(); self.root.mkdir(parents=True, exist_ok=True)
        for name in ("candidates", "approved", "signatures", "approvals"): (self.root / name).mkdir(exist_ok=True)
        self._variants, self._signatures, self.version = [], {}, 0
        self._load()
    def _safe_path(self, relative, namespace):
        pure = PurePosixPath(relative)
        if pure.is_absolute() or "\\" in relative or not pure.parts or pure.parts[0] != namespace or any(part in ("", ".", "..") for part in pure.parts): raise RegistryError("CANDIDATE_PATH_QUARANTINED")
        target = (self.root / Path(*pure.parts)).resolve(); base = (self.root / namespace).resolve()
        if target != base and base not in target.parents: raise RegistryError("CANDIDATE_PATH_QUARANTINED")
        return target
    def _write_once(self, target, data):
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with target.open("xb") as stream: stream.write(data); stream.flush(); os.fsync(stream.fileno())
        except FileExistsError:
            if target.read_bytes() != data: raise RegistryError("REGISTRY_CONFLICT")
    def register_candidate(self, candidate, code):
        if len(code) > MAX_CANDIDATE_BYTES or _hash(code) != candidate.code_sha256: raise RegistryError("REGISTRY_CONFLICT")
        target = self._safe_path(candidate.code_path, "candidates")
        with self._lock:
            self._write_once(target, code); self._write_once(self.root / "candidates" / f"{candidate.candidate_id}.json", _json(candidate.to_dict()).encode())
        return candidate
    def promote(self, candidate, approval, signature, code):
        if (approval.candidate_id != candidate.candidate_id or approval.parser_key != candidate.parser_key or approval.customer != candidate.customer or approval.candidate_hash != candidate.candidate_hash or approval.code_sha256 != candidate.code_sha256 or candidate.signature_hash != signature.signature_hash or candidate.rule_hash != signature.rule_hash or approval.signature_hash != signature.signature_hash or approval.rule_hash != signature.rule_hash or approval.fixture_hash != candidate.fixture_hash): raise RegistryError("REGISTRY_CONFLICT")
        if _hash(code) != candidate.code_sha256: raise RegistryError("REGISTRY_CONFLICT")
        candidate_path = self._safe_path(candidate.code_path, "candidates")
        metadata_path = self.root / "candidates" / f"{candidate.candidate_id}.json"
        if (not candidate_path.is_file() or not metadata_path.is_file()
            or candidate_path.read_bytes() != code
            or metadata_path.read_bytes() != _json(candidate.to_dict()).encode()):
            raise RegistryError("REGISTRY_CONFLICT")
        approved_path = f"approved/{candidate.parser_key}/{approval.variant_id}/{approval.version}/parse.py"
        variant = ApprovedVariant(candidate.parser_key, approval.variant_id, approval.version, approved_path, candidate.code_sha256, signature.signature_hash, signature.rule_hash, approval.approval_id)
        with self._lock:
            for old in self._variants:
                if (old.parser_key, old.variant_id, old.version) == (variant.parser_key, variant.variant_id, variant.version):
                    if old != variant: raise RegistryError("REGISTRY_CONFLICT")
                    return old
            self._write_once(self._safe_path(approved_path, "approved"), code)
            self._write_once(self.root / "signatures" / f"{signature.signature_hash}.json", _json(signature.to_dict()).encode())
            self._write_once(self.root / "approvals" / f"{approval.approval_id}.json", _json(approval.to_dict()).encode())
            self._variants.append(variant); self._signatures[signature.signature_hash] = signature; self.version += 1
            snapshot = _json({"version": self.version, "variants": [item.to_dict() for item in self._variants]}).encode()
            self._write_once(self.root / f"registry-{self.version}.json", snapshot)
            temporary = self.root / f"registry.tmp-{uuid.uuid4().hex}"; temporary.write_bytes(snapshot); os.replace(temporary, self.root / "registry.json")
        return variant
    def _load(self):
        pointer = self.root / "registry.json"
        if not pointer.exists(): return
        try:
            value = json.loads(pointer.read_text(encoding="utf-8")); self.version = int(value["version"])
            for item in value["variants"]:
                variant = ApprovedVariant(**item); signature = VariantSignature.from_dict(json.loads((self.root / "signatures" / f"{variant.signature_hash}.json").read_text()))
                code = self._safe_path(variant.code_path, "approved").read_bytes()
                if _hash(code) != variant.code_sha256: raise RegistryError("REGISTRY_CORRUPT")
                self._variants.append(variant); self._signatures[signature.signature_hash] = signature
        except RegistryError: raise
        except Exception as exc: raise RegistryError("REGISTRY_CORRUPT") from exc
    def registry(self): return MasterRegistry(self._variants, self._signatures)
