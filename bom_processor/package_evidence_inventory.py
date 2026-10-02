"""In-memory package inventory from supplied metadata and bytes; no file I/O.

Every accepted archive member has a byte-bound identity. Unsafe/incomplete input
fails closed. Run python -m pytest tests/test_package_evidence_inventory.py.
"""
from __future__ import annotations

import io
import re
import stat
import zipfile
from dataclasses import dataclass
from typing import Any

from .package_evidence_contracts import (
    PackageEvidenceError, bom_metadata, detached, digest, evidence_id, integer,
    member_path, require, strict, text,
)


@dataclass(frozen=True)
class InventoryLimits:
    depth: int = 5
    members: int = 10_000
    root_bytes: int = 50_000_000
    member_bytes: int = 50_000_000
    aggregate_bytes: int = 200_000_000
    ratio: int = 200
    evidence: int = 10_000
    excerpt_chars: int = 2000

    def __post_init__(self) -> None:
        ceilings = (5, 10_000, 50_000_000, 50_000_000, 200_000_000, 200, 10_000, 2000)
        for value, ceiling in zip(self.__dict__.values(), ceilings):
            integer(value, 1, ceiling)


class ArchiveBudget:
    def __init__(self, limits: InventoryLimits):
        self.limits = limits
        self.members = 0
        self.expanded = 0


def verified_bytes(metadata: dict, data: Any, maximum: int) -> bytes:
    bom_metadata(metadata)
    require(type(data) is bytes, "INVALID_BYTES")
    require(len(data) <= maximum, "BYTE_LIMIT")
    require(len(data) == metadata["sizeBytes"] and digest(data) == metadata["sha256"],
            "BYTE_IDENTITY_MISMATCH")
    return data


def read_zip(data: bytes, budget: ArchiveBudget) -> list[tuple[str, bytes]]:
    """Validate metadata before reading; enforce actual bytes and CRC during read.

    Directory records are validated/count toward limits, but are not file evidence.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = archive.infolist()
            budget.members += len(infos)
            require(budget.members <= budget.limits.members, "ARCHIVE_MEMBER_LIMIT")
            seen: set[str] = set()
            prefixes: dict[str, str] = {}
            file_paths = {info.filename for info in infos if not info.is_dir()}
            for info in infos:
                require(info.orig_filename == info.filename, "INVALID_MEMBER_PATH")
                name = info.filename[:-1] if info.is_dir() else info.filename
                member_path(name)
                folded = name.casefold()
                require(folded not in seen, "ARCHIVE_DUPLICATE_MEMBER")
                seen.add(folded)
                parts = name.split("/")
                for index in range(1, len(parts) + 1):
                    prefix = "/".join(parts[:index])
                    previous = prefixes.setdefault(prefix.casefold(), prefix)
                    require(previous == prefix, "ARCHIVE_CASE_ALIAS")
                    require(index == len(parts) or prefix not in file_paths,
                            "ARCHIVE_LOCATION_CONFLICT")
                mode = info.external_attr >> 16
                require(not stat.S_ISLNK(mode) and stat.S_IFMT(mode) in
                    ({0, stat.S_IFDIR} if info.is_dir() else {0, stat.S_IFREG}), "ARCHIVE_SPECIAL_MEMBER")
                require(not info.is_dir() or info.file_size == 0, "ARCHIVE_DIRECTORY_PAYLOAD")
                require(not info.flag_bits & (1 | 64), "ARCHIVE_ENCRYPTED")
                require(info.compress_type in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED},
                        "ARCHIVE_COMPRESSION")
                require(0 <= info.file_size <= budget.limits.member_bytes, "ARCHIVE_BYTE_LIMIT")
                require(info.file_size <= max(info.compress_size, 1) * budget.limits.ratio,
                        "ARCHIVE_RATIO_LIMIT")
                budget.expanded += info.file_size
                require(budget.expanded <= budget.limits.aggregate_bytes, "ARCHIVE_AGGREGATE_LIMIT")
            result = []
            for info in infos:
                if info.is_dir():
                    continue
                with archive.open(info) as stream:
                    payload = stream.read(budget.limits.member_bytes + 1)
                    require(len(payload) == info.file_size and not stream.read(1),
                            "ARCHIVE_SIZE_MISMATCH")
                result.append((info.filename, payload))
            return result
    except PackageEvidenceError:
        raise
    except (Exception, ) as exc:
        raise PackageEvidenceError("ARCHIVE_INVALID") from exc


def _safe_excerpt(data: bytes, maximum: int) -> str | None:
    # Inspect a bounded prefix, without interpreting engineering meaning.
    prefix = data[:16_384]
    try:
        value = prefix.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError:
        return None
    if any((ord(char) < 32 and char not in "\t\n\r") or ord(char) == 127
           or 0xD800 <= ord(char) <= 0xDFFF for char in value):
        return None
    return value[:maximum]


def describe(file_name: str, data: bytes, limits: InventoryLimits,
             budget: ArchiveBudget | None = None) -> dict:
    suffix = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else ""
    excerpt = _safe_excerpt(data, limits.excerpt_chars)
    kind = "unknown"
    if suffix in {"xlsx", "xlsm"} and data.startswith(b"PK"):
        parts = read_zip(data, budget or ArchiveBudget(limits))
        names = {name for name, _ in parts}
        if {"[Content_Types].xml", "xl/workbook.xml"} <= names:
            kind = "ooxml_" + suffix
        excerpt = None
    elif data.startswith(b"%PDF-"):
        kind, excerpt = "pdf", None
    elif data.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        kind, excerpt = "binary_xls", None
    elif suffix == "xls" and excerpt is not None and re.search(r"<table\b", excerpt, re.I):
        kind = "html_table_xls"
    elif excerpt is not None and re.search(r"%FS[LA]|%MO(?:IN|MM)|G04", excerpt):
        kind = "gerber_text"
    elif excerpt is not None and re.search(r"(?:^|\n)M48\b", excerpt):
        kind = "excellon_text"
    elif suffix == "zip":
        kind, excerpt = "zip_archive", None
    # Unknown text is an observation, never evidence of missing/malformed data.
    return {"fileName": file_name, "format": kind, "excerpt": excerpt}


def build_inventory(roots: list[dict], *, limits: InventoryLimits | None = None, observe=None) -> list[dict]:
    """Roots: documentId/sha256/sizeBytes/fileName/data. Return identity + description.

    Raw bytes never enter the returned inventory or the model packet.
    """
    limits = limits or InventoryLimits()
    require(type(roots) is list and len(roots) <= limits.evidence, "INVALID_ROOTS")
    budget = ArchiveBudget(limits)
    result: list[dict] = []
    locations: set[str] = set()

    # Count all supplied roots before expansion, so expansion cannot borrow the
    # remaining budget from roots that have not yet been visited.
    for root in roots:
        strict(root, {"documentId", "sha256", "sizeBytes", "fileName", "data"})
        require(type(root["data"]) is bytes, "INVALID_BYTES")
        budget.expanded += len(root["data"])
        require(budget.expanded <= limits.aggregate_bytes, "ROOT_AGGREGATE_LIMIT")

    def visit(metadata: dict, file_name: str, payload: bytes, chain: list[dict]) -> None:
        require(len(chain) <= limits.depth, "ARCHIVE_DEPTH_LIMIT")
        identity = {**metadata, "archiveChain": chain}
        description = describe(file_name, payload, limits, budget)
        if observe is not None:
            observe(detached(identity), payload, file_name)
        result.append({"evidenceId": evidence_id(identity), **identity,
                       "category": description["format"], **description})
        require(len(result) <= limits.evidence, "EVIDENCE_LIMIT")
        if file_name.lower().endswith(".zip"):
            require(payload.startswith((b"PK\x03\x04", b"PK\x05\x06")), "ARCHIVE_INVALID")
            members = read_zip(payload, budget)
            require(not members or len(chain) < limits.depth, "ARCHIVE_DEPTH_LIMIT")
            for name, member in members:
                visit(metadata, name, member, [*chain, {"memberPath": name,
                      "sha256": digest(member), "sizeBytes": len(member)}])

    for root in roots:
        strict(root, {"documentId", "sha256", "sizeBytes", "fileName", "data"})
        metadata = bom_metadata({key: root[key] for key in ("documentId", "sha256", "sizeBytes")})
        require(metadata["documentId"] not in locations, "DUPLICATE_ROOT_DOCUMENT")
        locations.add(metadata["documentId"])
        file_name = text(root["fileName"])
        payload = verified_bytes(metadata, root["data"], limits.root_bytes)
        visit(metadata, file_name, payload, [])
    return sorted(detached(result), key=lambda item: item["evidenceId"])