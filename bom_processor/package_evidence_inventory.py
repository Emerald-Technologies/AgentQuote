"""In-memory package inventory from supplied metadata and bytes; no file I/O.

Every accepted archive member has a byte-bound identity. Unsafe/incomplete input
fails closed. Files the reviewed relevance rules mark as known unused for quoting
(QuickQuote #134, D9: ODB++, design source, 3D models, duplicate CAD drawings, viewer
files) are still inventoried and hash-pinned, with their reason code as category, but
their bytes do not count against the aggregate budget. Package rule v2 (QuickQuote #99,
Nick's D2/D6): those files, and members over the per-file limit, are listed and
hash-pinned by streaming but never read; an over-limit member is a named warning, not a
stop; each FILE counts once (archives are containers); a hard cap bounds all inflated
bytes. QuickQuote computes the same selection (package-evidence-inventory.ts); its
parity test runs this module. Run python -m pytest tests/test_package_evidence_inventory.py.
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
# (plan 2026-10-07 section 5.2, Phase L1). MEMBER bounds one file held whole; AGGREGATE bounds the
# files read (each counted once); INFLATED is the hard cap on all decompression in one inventory (#99).
ROOT_BYTES_LIMIT = 100_000_000
MEMBER_BYTES_LIMIT = 50_000_000
AGGREGATE_BYTES_LIMIT = 500_000_000
INFLATED_BYTES_LIMIT = 2_000_000_000


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
    inflated_bytes: int = INFLATED_BYTES_LIMIT

    def __post_init__(self) -> None:
        ceilings = (5, 10_000, ROOT_BYTES_LIMIT, MEMBER_BYTES_LIMIT, AGGREGATE_BYTES_LIMIT, 200, 10_000, 2000,
                    INFLATED_BYTES_LIMIT)
        for value, ceiling in zip(self.__dict__.values(), ceilings):
            integer(value, 1, ceiling)


class ArchiveBudget:
    def __init__(self, limits: InventoryLimits):
        self.limits = limits
        self.members = 0
        self.expanded = 0
        self.inflated = 0


def verified_bytes(metadata: dict, data: Any, maximum: int) -> bytes:
    bom_metadata(metadata)
    require(type(data) is bytes, "INVALID_BYTES")
    require(len(data) <= maximum, "BYTE_LIMIT")
    require(len(data) == metadata["sizeBytes"] and digest(data) == metadata["sha256"],
            "BYTE_IDENTITY_MISMATCH")
    return data


def read_zip(data: bytes, budget: ArchiveBudget, uncounted=None) -> list[tuple[str, bytes]]:
    """Validate metadata before reading; enforce actual bytes and CRC during read.

    Directory records are validated/count toward limits, but are not file evidence.
    uncounted(name): members known to be unused for quoting; their bytes are not counted (#134).
    """
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = _validated_infos(archive, budget, uncounted=uncounted)
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


def _over_limit(info: zipfile.ZipInfo, limits: InventoryLimits) -> bool:
    """Over the per-file limit by declared or compressed size (same test as QuickQuote)."""
    return info.file_size > limits.member_bytes or info.compress_size > limits.member_bytes


def _read_member(archive: zipfile.ZipFile, info: zipfile.ZipInfo, limits: InventoryLimits) -> bytes:
    with archive.open(info) as stream:
        payload = stream.read(limits.member_bytes + 1)
        require(len(payload) == info.file_size and not stream.read(1), "ARCHIVE_SIZE_MISMATCH")
    return payload


def _validated_infos(archive: zipfile.ZipFile, budget: ArchiveBudget, *, aggregate: bool = True, uncounted=None,
                     allow_over_limit: bool = False, inflated: bool = False) -> list:
    """Every member record checked and counted; declared sizes reserved in budget.expanded.

    allow_over_limit (rule v2): a file over the per-file limit is allowed (listed, not read).
    inflated (rule v2): every file's declared size is reserved against the hard cap on inflated bytes.
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
        if allow_over_limit:
            require(not info.is_dir() or not _over_limit(info, budget.limits), "ARCHIVE_BYTE_LIMIT")
        else:
            require(0 <= info.file_size <= budget.limits.member_bytes, "ARCHIVE_BYTE_LIMIT")
        require(info.file_size <= max(info.compress_size, 1) * budget.limits.ratio,
                "ARCHIVE_RATIO_LIMIT")
        if inflated and not info.is_dir():
            budget.inflated += info.file_size
            require(budget.inflated <= budget.limits.inflated_bytes, "ARCHIVE_INFLATED_LIMIT")
        if uncounted is None or not uncounted(name):
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


def list_archive(data: bytes, limits: InventoryLimits) -> list[tuple[tuple[str, ...], int, bool]]:
    """Every file member of one root archive at any depth, as (member paths, declared size, over the per-file limit).

    Same member checks as read_zip (and QuickQuote listArchive under rule v2), except that a member over the per-file
    limit is listed rather than refused, and an archive over it is not opened. The aggregate budget is not applied.
    """
    budget = ArchiveBudget(limits)
    found: list[tuple[tuple[str, ...], int, bool]] = []

    def walk(payload: bytes, chain: tuple[str, ...]) -> None:
        require(payload.startswith((b"PK\x03\x04", b"PK\x05\x06")), "ARCHIVE_INVALID")
        try:
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                infos = _validated_infos(archive, budget, aggregate=False, allow_over_limit=True)
                require(not any(not i.is_dir() for i in infos) or len(chain) < limits.depth, "ARCHIVE_DEPTH_LIMIT")
                nested = []
                for info in infos:
                    if info.is_dir():
                        continue
                    over = _over_limit(info, limits)
                    found.append(((*chain, info.filename), info.file_size, over))
                    if info.filename.lower().endswith(".zip") and not over:
                        nested.append((info.filename, _read_member(archive, info, limits)))
        except PackageEvidenceError:
            raise
        except (Exception, ) as exc:
            raise PackageEvidenceError("ARCHIVE_INVALID") from exc
        for name, member in nested:
            walk(member, (*chain, name))

    walk(data, ())
    return found


def _file_key(document_id: str, member_paths) -> str:
    return "\x1f".join((document_id, *member_paths))


# Package rule v2 (QuickQuote #99; mirrors PACKAGE_EVIDENCE_INVENTORY_RULE_VERSION). Code-owned formats, also the
# category of a file listed but not read: over the per-file limit, or known unused (#134, whose category stays its
# reason code while it is not needed).
INVENTORY_RULE_VERSION = "package-evidence-inventory.v2"
NOT_READ_FORMAT = "file_not_read_size_limit"
NOT_READ_UNUSED_FORMAT = "file_not_read_known_unused"
NOT_READ_MESSAGES = {
    "MEMBER_NOT_READ": "Too large to read: over the per-file limit, so it is listed and hash-pinned but its contents "
                       "were not checked. Open it and map it by hand if quoting needs it.",
    "ONLY_FAB_DATA_NOT_READ": "Fab data too large to read: over the per-file limit, and no other fab file next to it "
                              "was read, so this board may have no readable fab data. Open it and check it by hand.",
}


def select_archives_v2(roots: list[dict], limits: InventoryLimits) -> dict:
    """Package rule v2 (QuickQuote #99 blockers 1 and 3), mirrored in QuickQuote limitsSelection.

    Each FILE counts once against the budget: archives (root or nested) are containers and do not count; their files
    do. Files the relevance rules mark not used (#134) and members over the per-file limit are listed, not read:
    hashed by streaming, never held whole, not counted, and an archive over the limit is not opened. An archive is
    unpacked only while its files fit the budget and everything inflated from it fits the hard cap; otherwise it is
    kept whole and reported. Returns unpack (documentIds), skipped (not-inventoried records), uncounted, unread and
    over_limit (file keys), disregarded (documentId, fileName, memberPaths, sizeBytes, kind, reasonCode, reason) and
    findings.
    """
    from .package_file_relevance import classify, path_segments
    files: list[dict] = []
    for root in roots:
        files.append({"documentId": root["documentId"], "fileName": root["fileName"], "memberPaths": (),
                      "size": len(root["data"]), "over": False})
        if text(root["fileName"]).lower().endswith(".zip"):
            files.extend({"documentId": root["documentId"], "fileName": root["fileName"], "memberPaths": paths,
                          "size": size, "over": over} for paths, size, over in list_archive(root["data"], limits))
    results, findings = classify([path_segments((item["fileName"], *item["memberPaths"])) for item in files])
    uncounted: set[str] = set()
    unread: set[str] = set()
    over_limit: set[str] = set()
    disregarded = []
    counted: dict[str, int] = {}
    inflated: dict[str, int] = {}
    root_bytes = 0
    for item, label in zip(files, results):
        key = _file_key(item["documentId"], item["memberPaths"])
        if label is not None:
            disregarded.append({"documentId": item["documentId"], "fileName": item["fileName"],
                                "memberPaths": list(item["memberPaths"]), "sizeBytes": item["size"], **label})
        name = item["memberPaths"][-1] if item["memberPaths"] else item["fileName"]
        container = name.lower().endswith(".zip") and not item["over"]
        not_read = not container and (item["over"] or label is not None)
        if item["over"]:
            over_limit.add(key)
        if not_read:
            unread.add(key)
        if container or not_read:
            uncounted.add(key)
        if not item["memberPaths"]:
            if not container and not not_read:
                root_bytes += item["size"]
            continue
        inflated[item["documentId"]] = inflated.get(item["documentId"], 0) + item["size"]
        if not container and not not_read:
            counted[item["documentId"]] = counted.get(item["documentId"], 0) + item["size"]
    room = limits.aggregate_bytes - root_bytes
    measured = []
    for root in roots:
        if not text(root["fileName"]).lower().endswith(".zip"):
            continue
        size, work = counted.get(root["documentId"], 0), inflated.get(root["documentId"], 0)
        measured.append((root, size if 0 <= room and size <= room and work <= limits.inflated_bytes else None, work))
    # Smallest first (ties by documentId) while the files fit the budget and the inflated bytes fit the hard cap.
    fitting = sorted((size, root["documentId"], work) for root, size, work in measured if size is not None)
    unpack, used, spent = set(), 0, 0
    for size, document_id, work in fitting:
        if used + size <= room and spent + work <= limits.inflated_bytes:
            used += size
            spent += work
            unpack.add(document_id)
    skipped = [{"documentId": root["documentId"], "sha256": root["sha256"], "sizeBytes": root["sizeBytes"],
                "fileName": root["fileName"], "expandedBytes": size, "reason": NOT_INVENTORIED_REASON}
               for root, size, _ in measured if root["documentId"] not in unpack]
    disregarded.sort(key=lambda item: _file_key(item["documentId"], item["memberPaths"]))
    return {"unpack": unpack, "skipped": sorted(skipped, key=lambda item: item["documentId"]),
            "uncounted": uncounted, "unread": unread, "over_limit": over_limit, "disregarded": disregarded,
            "findings": findings}


def _looks_like_fab_data(low: list[str]) -> bool:
    """Clues, not rules (the reviewed relevance clues; mirrors QuickQuote looksLikeFabData): a fab extension, a fab
    word in the file name, or a folder or archive named exactly like an ODB++ root. low: lower-case path segments."""
    from .package_file_relevance import RULES
    clues = RULES["fabClues"]
    name = low[-1]
    dot = name.rfind(".")
    extension = name[dot:] if dot > 0 else ""
    if (extension in clues["extensions"] or re.search(clues["extensionPattern"], extension)
            or re.search(clues["namePattern"], name)):
        return True
    for segment in low:
        suffix = next((item for item in RULES["archiveExtensions"]
                       if segment.endswith(item) and len(segment) > len(item)), None)
        stem = segment[:-len(suffix)] if suffix else segment
        if (stem[:-4] if stem.endswith(".tar") and len(stem) > 4 else stem) in RULES["odbpp"]["rootNames"]:
            return True
    return False


def not_read_findings(files: list[dict]) -> list[dict]:
    """One warning per member listed, not read for its size (QuickQuote #99, D2), sorted by file key. Louder
    (ONLY_FAB_DATA_NOT_READ) when it looks like fab data and no fab-looking file in the same folder was read.
    files: documentId, fileName (uploaded root), memberPaths, sizeBytes, state (read / over_limit / other)."""
    from .package_file_relevance import path_segments
    lows = [[segment.lower() for segment in path_segments((item["fileName"], *item["memberPaths"]))] for item in files]
    fab = [_looks_like_fab_data(low) for low in lows]
    found = []
    for index, item in enumerate(files):
        if item["state"] != "over_limit":
            continue
        folder = lows[index][:-1]
        only = fab[index] and not any(
            other["state"] == "read" and fab[at] and len(lows[at]) > len(folder) and lows[at][:len(folder)] == folder
            for at, other in enumerate(files))
        code = "ONLY_FAB_DATA_NOT_READ" if only else "MEMBER_NOT_READ"
        found.append({"documentId": item["documentId"], "fileName": item["fileName"],
                      "memberPaths": list(item["memberPaths"]), "sizeBytes": item["sizeBytes"], "code": code,
                      "severity": "warning", "message": NOT_READ_MESSAGES[code]})
    return sorted(found, key=lambda item: _file_key(item["documentId"], item["memberPaths"]))


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


def _stream_digest(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> str:
    """SHA-256 of a member listed, not read: streamed in bounded chunks, size- and CRC-checked, never held whole."""
    value = hashlib.sha256()
    seen = 0
    with archive.open(info) as stream:
        while chunk := stream.read(1 << 20):
            seen += len(chunk)
            require(seen <= info.file_size, "ARCHIVE_SIZE_MISMATCH")
            value.update(chunk)
    require(seen == info.file_size, "ARCHIVE_SIZE_MISMATCH")
    return value.hexdigest()


def _members(payload: bytes, budget: ArchiveBudget, depth: int, uncounted, unread):
    """One archive's file members, one at a time (QuickQuote #99): (name, bytes or None when listed, not read, sha256,
    size). A member is released once the caller moves on, so a nested archive's buffer is freed after it is visited."""
    require(payload.startswith((b"PK\x03\x04", b"PK\x05\x06")), "ARCHIVE_INVALID")
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            infos = _validated_infos(archive, budget, uncounted=uncounted, allow_over_limit=True, inflated=True)
            require(not any(not i.is_dir() for i in infos) or depth < budget.limits.depth, "ARCHIVE_DEPTH_LIMIT")
            for info in infos:
                if info.is_dir():
                    continue
                if unread(info.filename):
                    yield info.filename, None, _stream_digest(archive, info), info.file_size
                    continue
                require(not _over_limit(info, budget.limits), "ARCHIVE_BYTE_LIMIT")
                member = _read_member(archive, info, budget.limits)
                yield info.filename, member, digest(member), len(member)
    except PackageEvidenceError:
        raise
    except (Exception, ) as exc:
        raise PackageEvidenceError("ARCHIVE_INVALID") from exc


def build_inventory_report(roots: list[dict], *, limits: InventoryLimits | None = None, observe=None) -> dict:
    """Roots: documentId/sha256/sizeBytes/fileName/data. Returns inventory (identity + description, sorted by
    evidenceId), notInventoried, disregarded, notRead (warnings), countedBytes and inflatedBytes.

    Raw bytes never enter the returned inventory or the model packet. observe(identity, bytes or None, name):
    None for a file listed, not read (QuickQuote passes null the same way).
    """
    limits = limits or InventoryLimits()
    require(type(roots) is list and len(roots) <= limits.evidence, "INVALID_ROOTS")
    budget = ArchiveBudget(limits)
    result: list[dict] = []
    locations: set[str] = set()

    for root in roots:
        strict(root, {"documentId", "sha256", "sizeBytes", "fileName", "data"})
        require(type(root["data"]) is bytes, "INVALID_BYTES")

    # Root archives that would exceed the aggregate budget stay one pinned evidence object
    # with a code-owned format (QuickQuote computes the identical selection). Known-unused
    # files (#134) keep their reason code as category; under rule v2 (#99) they, and members
    # over the per-file limit, are listed and hash-pinned but never read.
    verified = []
    for root in roots:
        metadata = bom_metadata({key: root[key] for key in ("documentId", "sha256", "sizeBytes")})
        require(metadata["documentId"] not in locations, "DUPLICATE_ROOT_DOCUMENT")
        locations.add(metadata["documentId"])
        verified.append((metadata, text(root["fileName"]), verified_bytes(metadata, root["data"], limits.root_bytes)))
    selection = select_archives_v2(roots, limits)
    unpack, uncounted, unread = selection["unpack"], selection["uncounted"], selection["unread"]
    labels = {_file_key(item["documentId"], item["memberPaths"]): item for item in selection["disregarded"]}

    # Count the supplied roots' files before expansion, so expansion cannot borrow the
    # remaining budget from roots that have not yet been visited. Archives are containers: not counted.
    for root in roots:
        if _file_key(root["documentId"], ()) in uncounted:
            continue
        budget.expanded += len(root["data"])
        require(budget.expanded <= limits.aggregate_bytes, "ROOT_AGGREGATE_LIMIT")

    names = {metadata["documentId"]: file_name for metadata, file_name, _ in verified}
    states: list[dict] = []

    def visit(metadata: dict, file_name: str, payload: bytes | None, chain: list[dict]) -> None:
        require(len(chain) <= limits.depth, "ARCHIVE_DEPTH_LIMIT")
        identity = {**metadata, "archiveChain": chain}
        paths = [step["memberPath"] for step in chain]
        key = _file_key(metadata["documentId"], paths)
        not_read = key in unread
        set_aside = not chain and file_name.lower().endswith(".zip") and metadata["documentId"] not in unpack
        # Workbook parts are read for format detection only, with their own transient budget:
        # they are not inventory members (QuickQuote does not count them either).
        if not_read:
            description = {"fileName": file_name, "excerpt": None,
                           "format": NOT_READ_FORMAT if key in selection["over_limit"] else NOT_READ_UNUSED_FORMAT}
        elif set_aside:
            description = {"fileName": file_name, "format": NOT_INVENTORIED_FORMAT, "excerpt": None}
        else:
            description = describe(file_name, payload, limits)
        if observe is not None:
            observe(detached(identity), None if not_read else payload, file_name)
        label = labels.get(key)
        result.append({"evidenceId": evidence_id(identity), **identity,
                       "category": label["reasonCode"] if label else description["format"], **description})
        require(len(result) <= limits.evidence, "EVIDENCE_LIMIT")
        over = key in selection["over_limit"] and label is None
        states.append({"documentId": metadata["documentId"], "fileName": names[metadata["documentId"]],
                       "memberPaths": paths, "sizeBytes": (chain[-1] if chain else metadata)["sizeBytes"],
                       "evidenceId": result[-1]["evidenceId"],
                       "state": "over_limit" if over else "other" if key in uncounted else "read"})
        if file_name.lower().endswith(".zip") and not set_aside and not not_read:
            for name, member, sha256, size in _members(
                    payload, budget, len(chain),
                    uncounted=lambda entry: _file_key(metadata["documentId"], (*paths, entry)) in uncounted,
                    unread=lambda entry: _file_key(metadata["documentId"], (*paths, entry)) in unread):
                visit(metadata, name, member, [*chain, {"memberPath": name, "sha256": sha256, "sizeBytes": size}])

    for metadata, file_name, payload in verified:
        visit(metadata, file_name, None if _file_key(metadata["documentId"], ()) in unread else payload, [])
    listed = {_file_key(item["documentId"], [step["memberPath"] for step in item["archiveChain"]]) for item in result}
    evidence = {_file_key(item["documentId"], item["memberPaths"]): item["evidenceId"] for item in states}
    return {"inventory": sorted(detached(result), key=lambda item: item["evidenceId"]),
            "notInventoried": selection["skipped"],
            "disregarded": [item for item in selection["disregarded"]
                            if _file_key(item["documentId"], item["memberPaths"]) in listed],
            "notRead": [{**finding, "evidenceId": evidence[_file_key(finding["documentId"], finding["memberPaths"])]}
                        for finding in not_read_findings(states)],
            "rule": INVENTORY_RULE_VERSION, "findings": selection["findings"],
            "countedBytes": budget.expanded, "inflatedBytes": budget.inflated}


def build_inventory(roots: list[dict], *, limits: InventoryLimits | None = None, observe=None) -> list[dict]:
    """Roots: documentId/sha256/sizeBytes/fileName/data. Return identity + description (see build_inventory_report)."""
    return build_inventory_report(roots, limits=limits, observe=observe)["inventory"]
