"""In-memory package inventory from supplied metadata and bytes; no file I/O.

Every accepted archive member has a byte-bound identity. Unsafe/incomplete input
fails closed. Run python -m pytest tests/test_package_evidence_inventory.py.
"""
from __future__ import annotations

import hashlib
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


# Single source for evidence byte limits; mirrors QuickQuote PACKAGE_EVIDENCE_INVENTORY_LIMITS
# (plan 2026-10-07 section 5.2, Phase L1). Whole buffers are held, so these bound memory.
ROOT_BYTES_LIMIT = 100_000_000
MEMBER_BYTES_LIMIT = 50_000_000
AGGREGATE_BYTES_LIMIT = 500_000_000


@dataclass(frozen=True)
class InventoryLimits:
    depth: int = 5
    members: int = 10_000
    root_bytes: int = ROOT_BYTES_LIMIT
    member_bytes: int = MEMBER_BYTES_LIMIT
    aggregate_bytes: int = AGGREGATE_BYTES_LIMIT
    ratio: int = 200
    evidence: int = 10_000
    excerpt_chars: int = 2000

    def __post_init__(self) -> None:
        ceilings = (5, 10_000, ROOT_BYTES_LIMIT, MEMBER_BYTES_LIMIT, AGGREGATE_BYTES_LIMIT, 200, 10_000, 2000)
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
            infos = _validated_infos(archive, budget)
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


def read_zip_listing(data: bytes, budget: ArchiveBudget,
                     hash_room: list[int]) -> list[tuple[str, bytes | None, str | None, int, str | None]]:
    """The package survey's reading (QQ #99 blocker 5): read_zip, except one member over the per-member
    byte or ratio limit no longer fails the whole archive. That member is listed, not read.

    Returns (name, payload, sha256, size, code) per file member. A read member has its payload and no
    code. A member listed, not read, has payload None and its limit code. Its bytes are streamed once
    for their SHA-256 and true size and are never held. hash_room[0] bounds the bytes streamed that way
    across the package; a member past it fails the archive with its limit code, as before. Every other
    check (paths, duplicates, count, aggregate, encryption, compression, CRC) still fails the archive.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            over: dict[str, str] = {}
            infos = _validated_infos(archive, budget, over_limit=over)
            result: list[tuple[str, bytes | None, str | None, int, str | None]] = []
            for info in infos:
                if info.is_dir():
                    continue
                if info.filename in over:
                    code = over[info.filename]
                    require(info.file_size <= hash_room[0], code)
                    hash_room[0] -= info.file_size
                    hasher, size = hashlib.sha256(), 0
                    with archive.open(info) as stream:
                        while chunk := stream.read(1 << 20):
                            size += len(chunk)
                            # The declared size bounds the stream: a member that lies about it stops here.
                            require(size <= info.file_size, "ARCHIVE_SIZE_MISMATCH")
                            hasher.update(chunk)
                    require(size == info.file_size, "ARCHIVE_SIZE_MISMATCH")
                    result.append((info.filename, None, hasher.hexdigest(), size, code))
                    continue
                with archive.open(info) as stream:
                    payload = stream.read(budget.limits.member_bytes + 1)
                    require(len(payload) == info.file_size and not stream.read(1),
                            "ARCHIVE_SIZE_MISMATCH")
                result.append((info.filename, payload, None, len(payload), None))
            return result
    except PackageEvidenceError:
        raise
    except (Exception, ) as exc:
        raise PackageEvidenceError("ARCHIVE_INVALID") from exc


def _validated_infos(archive: zipfile.ZipFile, budget: ArchiveBudget, *, aggregate: bool = True,
                     over_limit: dict[str, str] | None = None) -> list:
    """Every member record checked and counted; declared sizes reserved in budget.expanded.

    over_limit (the survey's listing only): a file member over the per-member byte or ratio limit is
    recorded there with its code and not reserved, instead of failing the archive.
    """
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
        if over_limit is not None and not info.is_dir():
            code = ("ARCHIVE_BYTE_LIMIT" if info.file_size > budget.limits.member_bytes
                    else "ARCHIVE_RATIO_LIMIT" if info.file_size > max(info.compress_size, 1) * budget.limits.ratio
                    else None)
            if code is not None:
                over_limit[info.filename] = code
                continue
        require(0 <= info.file_size <= budget.limits.member_bytes, "ARCHIVE_BYTE_LIMIT")
        require(info.file_size <= max(info.compress_size, 1) * budget.limits.ratio,
                "ARCHIVE_RATIO_LIMIT")
        budget.expanded += info.file_size
        require(not aggregate or budget.expanded <= budget.limits.aggregate_bytes,
                "ARCHIVE_AGGREGATE_LIMIT")
    return infos


# Recorded for a root archive whose members were not inventoried (mirrors QuickQuote
# PACKAGE_EVIDENCE_NOT_INVENTORIED_REASON). Also its code-owned evidence category.
NOT_INVENTORIED_REASON = "INVENTORY_SIZE_BUDGET"
NOT_INVENTORIED_FORMAT = "archive_not_unpacked_size_budget"


def measure_archive(data: bytes, limits: InventoryLimits, room: int) -> int | None:
    """Declared unpacked bytes of one root archive, counted exactly as read_zip counts them.

    Every member check except the aggregate byte budget still applies and raises. Returns
    None once the total exceeds room; nested archives are read only while within room.
    """
    budget = ArchiveBudget(limits)

    def walk(payload: bytes, depth: int) -> bool:
        require(payload.startswith((b"PK", b"PK")), "ARCHIVE_INVALID")
        try:
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                infos = _validated_infos(archive, budget, aggregate=False)
                if budget.expanded > room:
                    return False
                require(not any(not i.is_dir() for i in infos) or depth < limits.depth, "ARCHIVE_DEPTH_LIMIT")
                for info in infos:
                    if info.is_dir() or not info.filename.lower().endswith(".zip"):
                        continue
                    with archive.open(info) as stream:
                        member = stream.read(limits.member_bytes + 1)
                        require(len(member) == info.file_size and not stream.read(1), "ARCHIVE_SIZE_MISMATCH")
                    if not walk(member, depth + 1):
                        return False
                return True
        except PackageEvidenceError:
            raise
        except (Exception, ) as exc:
            raise PackageEvidenceError("ARCHIVE_INVALID") from exc

    return budget.expanded if walk(data, 0) else None


def select_archives(roots: list[dict], limits: InventoryLimits) -> tuple[set[str], list[dict]]:
    """Budget-aware selection shared with QuickQuote (package-evidence-inventory.ts).

    Every root is inventoried and counted first. Root archives are measured alone against the
    room left after all roots, then unpacked smallest-first (ties by documentId) while they fit.
    Returns (documentIds to unpack, not-inventoried records sorted by documentId).
    """
    room = limits.aggregate_bytes - sum(len(root["data"]) for root in roots)
    measured = [(root, measure_archive(root["data"], limits, room)) for root in roots
                if text(root["fileName"]).lower().endswith(".zip")]
    fitting = sorted(((size, root["documentId"]) for root, size in measured if size is not None))
    unpack, used = set(), 0
    for size, document_id in fitting:
        if used + size <= room:
            used += size
            unpack.add(document_id)
    skipped = [{"documentId": root["documentId"], "sha256": root["sha256"], "sizeBytes": root["sizeBytes"],
                "fileName": root["fileName"], "expandedBytes": size, "reason": NOT_INVENTORIED_REASON}
               for root, size in measured if root["documentId"] not in unpack]
    return unpack, sorted(skipped, key=lambda item: item["documentId"])


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

    # Root archives that would exceed the aggregate budget stay one pinned evidence object
    # with a code-owned format (QuickQuote computes the identical selection).
    verified = []
    for root in roots:
        metadata = bom_metadata({key: root[key] for key in ("documentId", "sha256", "sizeBytes")})
        require(metadata["documentId"] not in locations, "DUPLICATE_ROOT_DOCUMENT")
        locations.add(metadata["documentId"])
        verified.append((metadata, text(root["fileName"]), verified_bytes(metadata, root["data"], limits.root_bytes)))
    unpack, _skipped = select_archives(roots, limits)

    def visit(metadata: dict, file_name: str, payload: bytes, chain: list[dict]) -> None:
        require(len(chain) <= limits.depth, "ARCHIVE_DEPTH_LIMIT")
        identity = {**metadata, "archiveChain": chain}
        set_aside = not chain and file_name.lower().endswith(".zip") and metadata["documentId"] not in unpack
        # Workbook parts are read for format detection only, with their own transient budget:
        # they are not inventory members (QuickQuote does not count them either).
        description = ({"fileName": file_name, "format": NOT_INVENTORIED_FORMAT, "excerpt": None}
                       if set_aside else describe(file_name, payload, limits))
        if observe is not None:
            observe(detached(identity), payload, file_name)
        result.append({"evidenceId": evidence_id(identity), **identity,
                       "category": description["format"], **description})
        require(len(result) <= limits.evidence, "EVIDENCE_LIMIT")
        if file_name.lower().endswith(".zip") and not set_aside:
            require(payload.startswith((b"PK\x03\x04", b"PK\x05\x06")), "ARCHIVE_INVALID")
            members = read_zip(payload, budget)
            require(not members or len(chain) < limits.depth, "ARCHIVE_DEPTH_LIMIT")
            for name, member in members:
                visit(metadata, name, member, [*chain, {"memberPath": name,
                      "sha256": digest(member), "sizeBytes": len(member)}])

    for metadata, file_name, payload in verified:
        visit(metadata, file_name, payload, [])
    return sorted(detached(result), key=lambda item: item["evidenceId"])