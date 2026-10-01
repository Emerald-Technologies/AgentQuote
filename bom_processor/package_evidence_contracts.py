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