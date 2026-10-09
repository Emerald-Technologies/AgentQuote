"""Byte-derived semantic facts shared by validation and package review."""
from __future__ import annotations

import hashlib
import io
import re
from dataclasses import dataclass
from typing import Any

from .canonical_format import pair_indexes
from .semantic import AssemblyIdentity, normalize_bom


class SemanticFactsError(ValueError):
    """Parsed output has ambiguous assembly facts."""


@dataclass(frozen=True)
class SemanticFacts:
    rows: tuple[dict[str, Any], ...]
    assembly: AssemblyIdentity | None
    output_sha256: str
    fingerprint: str
    model_summary: dict[str, int]


def _missing(value: Any) -> bool:
    if value is None:
        return True
    try:
        import pandas as pd
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def _category(value: Any) -> str:
    if _missing(value):
        return ""
    return re.sub(r"[^A-Z0-9]+", "", str(value).upper())


def document_assembly(rows: list[dict[str, Any]] | tuple[dict[str, Any], ...]) -> AssemblyIdentity | None:
    """Return the one unique Document-row identity, or fail on disagreement."""
    identities: set[tuple[str, str]] = set()
    for row in rows:
        if _category(row.get("item_category")) != "DOCUMENT":
            continue
        name = "" if _missing(row.get("item_number")) else str(row.get("item_number")).strip()
        revision = "" if _missing(row.get("revision")) else str(row.get("revision")).strip()
        if name:
            identities.add(AssemblyIdentity(name, revision).normalized())
    if len(identities) > 1:
        raise SemanticFactsError("ASSEMBLY_IDENTITY_CONFLICT")
    if not identities:
        return None
    name, revision = next(iter(identities))
    return AssemblyIdentity(name, revision)


def facts_from_rows(rows: list[dict[str, Any]], *, output_sha256: str = "",
                    infer_document_assembly: bool = True) -> SemanticFacts:
    assembly = document_assembly(rows) if infer_document_assembly else None
    model = normalize_bom(rows, assembly or AssemblyIdentity("", ""))
    mpn_count = sum(
        1
        for row in rows
        for index in pair_indexes(row)
        if not _missing(row.get(f"manufacturer_item_number_{index}"))
        and bool(str(row.get(f"manufacturer_item_number_{index}")).strip())
    )
    return SemanticFacts(tuple(dict(row) for row in rows), assembly, output_sha256,
                         model.fingerprint,
                         {"component_count": len(model.components), "mpn_count": mpn_count})


def facts_from_xlsx(data: bytes) -> SemanticFacts:
    import pandas as pd
    rows = pd.read_excel(io.BytesIO(data), engine="openpyxl").to_dict(orient="records")
    return facts_from_rows(rows, output_sha256=hashlib.sha256(data).hexdigest())


def facts_dict(data: bytes) -> dict[str, Any]:
    facts = facts_from_xlsx(data)
    identity = None
    if facts.assembly is not None:
        name, revision = facts.assembly.normalized()
        identity = {"name": name, "revision": revision}
    return {"assembly_identity": identity, "mapping_required": identity is None,
            "fingerprint": facts.fingerprint, "model_summary": facts.model_summary,
            "output_sha256": facts.output_sha256}
