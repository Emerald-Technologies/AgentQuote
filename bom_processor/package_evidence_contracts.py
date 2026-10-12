"""Offline graph primitives. Inputs are metadata; outputs are detached JSON values.

No storage or execution. Verify with python -m pytest tests/test_package_evidence*.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any


class PackageEvidenceError(ValueError):
    """Stable, non-source-bearing validation error."""


_CAUSE_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,79}")
_CAUSE_CLASS = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,79}")


def failure_causes(exc: BaseException, limit: int = 8) -> list[str]:
    """Content-free cause chain for operators.

    PackageEvidenceError carries stable codes only; any other exception contributes its
    class name only (never its message, which can hold package data or provider bodies).
    `raise ... from None` is honoured, so deliberately hidden provider details stay hidden.
    """
    causes, seen, current = [], set(), exc
    while current is not None and len(causes) < limit and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, PackageEvidenceError):
            message = str(current)
            causes.append(message if _CAUSE_CODE.fullmatch(message) else "UNSTRUCTURED_EVIDENCE_ERROR")
        else:
            name = type(current).__name__
            causes.append(name if _CAUSE_CLASS.fullmatch(name) else "Exception")
        current = current.__cause__ or (None if current.__suppress_context__ else current.__context__)
    return causes


# Producer policy capability sets. v5 = v4 (retained provenance) plus independent reviewer artifact.
# v6 = v5 with the compact packet, file views, grouped replies and per-assembly calls (plan 8.8 step 5).
CLASSIFIED_POLICIES = frozenset({"package-evidence-foundry.v2", "package-evidence-foundry.v3",
                                 "package-evidence-foundry.v4", "package-evidence-foundry.v5",
                                 "package-evidence-foundry.v6"})
ASSESSED_POLICIES = frozenset({"package-evidence-foundry.v3", "package-evidence-foundry.v4", "package-evidence-foundry.v5",
                               "package-evidence-foundry.v6"})
RETAINED_POLICIES = frozenset({"package-evidence-foundry.v4", "package-evidence-foundry.v5", "package-evidence-foundry.v6"})
REVIEWED_POLICIES = frozenset({"package-evidence-foundry.v5", "package-evidence-foundry.v6"})
COMPACT_POLICIES = frozenset({"package-evidence-foundry.v6"})
# Code-owned relationship conflict (Nick 2026-10-12, decision 6): a file with no quotable text linked
# from its survey clue, folder and name. Never accepted without a person; model replies cannot set it.
LINK_NOT_TEXT_VERIFIED = "LINK_NOT_TEXT_VERIFIED"
# Code-owned requirement conflict (Nick 2026-10-12): every quote the model gave for it failed the exact-text
# check, so it is left open for a person (determination unknown, no citations). Model replies cannot set it.
QUOTE_UNVERIFIED = "EVIDENCE_QUOTE_UNVERIFIED"


def require(condition: bool, code: str) -> None:
    if not condition:
        raise PackageEvidenceError(code)


def json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=True, allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, RecursionError) as exc:
        raise PackageEvidenceError("INVALID_JSON") from exc


def detached(value: Any) -> Any:
    return json.loads(json_bytes(value))


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def object_hash(value: Any) -> str:
    return digest(json_bytes(value))


def strict(value: Any, fields: set[str], code: str = "INVALID_FIELDS") -> dict:
    require(type(value) is dict and set(value) == fields, code)
    return value


def integer(value: Any, minimum: int = 0, maximum: int = 2**53 - 1) -> int:
    require(type(value) is int and minimum <= value <= maximum, "INVALID_INTEGER")
    return value


def hash_value(value: Any) -> str:
    require(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None,
            "INVALID_HASH")
    return value


def uuid_value(value: Any) -> str:
    require(isinstance(value, str) and re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", value)
        is not None, "INVALID_UUID")
    return value


def utf16_units(value: str) -> int:
    """JavaScript/Zod string length, without changing the supplied Unicode text."""
    return sum(2 if ord(char) > 0xFFFF else 1 for char in value)


def text(value: Any, maximum: int = 256, *, controls: bool = False) -> str:
    require(isinstance(value, str) and 0 < utf16_units(value) <= maximum and bool(value.strip()),
            "INVALID_TEXT")
    require(controls or re.search(r"[\x00-\x1f\x7f]", value) is None, "INVALID_TEXT")
    # Reject isolated surrogates; valid Unicode is serialized as ASCII escapes.
    require(not any(0xD800 <= ord(char) <= 0xDFFF for char in value), "INVALID_TEXT")
    return value


def nullable_text(value: Any, maximum: int = 256, *, controls: bool = False) -> str | None:
    return None if value is None else text(value, maximum, controls=controls)


def member_path(value: Any) -> str:
    text(value, 1024)
    require(not re.search(r"[\\:]", value) and "!/" not in value
            and all(part and part not in {".", ".."} for part in value.split("/")),
            "INVALID_MEMBER_PATH")
    return value


def bom_metadata(value: Any) -> dict:
    strict(value, {"documentId", "sha256", "sizeBytes"})
    uuid_value(value["documentId"])
    hash_value(value["sha256"])
    integer(value["sizeBytes"])
    return detached(value)


def assemblies_metadata(values: Any) -> list[dict]:
    require(type(values) is list and 1 <= len(values) <= 50, "INVALID_ASSEMBLIES")
    ids = set()
    locations = {}
    for value in values:
        strict(value, {"assemblyUuid", "partNumber", "revision", "canonicalBom"})
        identity = uuid_value(value["assemblyUuid"])
        require(identity not in ids, "DUPLICATE_ASSEMBLY")
        ids.add(identity)
        text(value["partNumber"])
        nullable_text(value["revision"])
        if value["canonicalBom"] is not None:
            bom = bom_metadata(value["canonicalBom"])
            previous = locations.setdefault(bom["documentId"], bom)
            require(previous == bom, "CONFLICTING_BOM_METADATA")
    return sorted(detached(values), key=lambda item: item["assemblyUuid"])


def evidence_id(identity: dict) -> str:
    strict(identity, {"documentId", "sha256", "sizeBytes", "archiveChain"})
    bom_metadata({key: identity[key] for key in ("documentId", "sha256", "sizeBytes")})
    chain = identity["archiveChain"]
    require(type(chain) is list and len(chain) <= 5, "INVALID_ARCHIVE_CHAIN")
    for step in chain:
        strict(step, {"memberPath", "sha256", "sizeBytes"})
        member_path(step["memberPath"])
        hash_value(step["sha256"])
        integer(step["sizeBytes"])
    return object_hash({"version": "package-evidence-object.v1", **identity})


def occurrence_id(assembly_uuid: str, bom_sha256: str, sheet: str, row: int) -> str:
    uuid_value(assembly_uuid)
    hash_value(bom_sha256)
    text(sheet)
    integer(row, 1)
    return object_hash({"version": "package-bom-occurrence.v1", "assemblyUuid": assembly_uuid,
                        "bomSha256": bom_sha256, "sheet": sheet, "row": row})


def semantic_key(part_number: str, revision: str | None) -> str:
    text(part_number)
    nullable_text(revision)
    return object_hash({"version": "package-part-match.v1", "partNumber": part_number,
                        "revision": revision})


def association_id(value: dict) -> str:
    return object_hash({"version": "package-evidence-association.v1", **value})