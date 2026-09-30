"""Pure semantic comparison for parsed BOMs.

The module intentionally accepts only already-parsed rows and assembly metadata. It
performs no file, network, or dataframe I/O and uses only the Python standard
library.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping

_MAX_EVIDENCE = 40
_MAX_VALUE_LENGTH = 160


def _norm(value: Any) -> str:
    if value is None:
        return ""
    text = unicodedata.normalize("NFKC", str(value)).strip()
    return " ".join(text.split()).casefold()


def _number(value: Any) -> str:
    if value is None or (isinstance(value, float) and value != value):
        return ""
    text = _norm(value)
    try:
        number = Decimal(text)
    except InvalidOperation:
        return text
    return format(number.normalize(), "f")


def _key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", _norm(value))


def _first(row: Mapping[str, Any], names: tuple[str, ...]) -> Any:
    values = {_key(k): value for k, value in row.items()}
    for name in names:
        if _key(name) in values:
            return values[_key(name)]
    return None


def _refs(value: Any) -> tuple[str, ...]:
    """Normalize refdes ordering without inventing ranges.

    A range is treated as a token (rather than expanded), so R1-R3 and R1,R2,R3
    remain distinguishable unless the source explicitly provides the same facts.
    """
    text = _norm(value)
    if not text:
        return ()
    tokens = [token for token in re.split(r"[,;\s]+", text) if token]
    return tuple(sorted(set(tokens)))


@dataclass(frozen=True)
class AssemblyIdentity:
    name: str
    revision: str = ""

    def normalized(self) -> tuple[str, str]:
        return _norm(self.name), _norm(self.revision)


@dataclass(frozen=True)
class Component:
    identity: tuple[str, str]
    material: tuple[tuple[str, Any], ...]
    optional: tuple[tuple[str, Any], ...]

    def canonical(self) -> dict[str, Any]:
        return {"identity": self.identity, "material": self.material, "optional": self.optional}


@dataclass(frozen=True)
class BomSemantic:
    assembly: tuple[str, str]
    components: tuple[Component, ...]
    fingerprint: str


@dataclass(frozen=True)
class BomComparison:
    relation: str
    left_fingerprint: str
    right_fingerprint: str
    evidence: tuple[dict[str, Any], ...]

    @property
    def equivalent(self) -> bool:
        return self.relation == "equivalent"

    @property
    def dominates(self) -> bool:
        return self.relation == "dominates"

    @property
    def conflict(self) -> bool:
        return self.relation == "conflict"


def semantic_summary(rows: Iterable[Mapping[str, Any]], *, assembly: AssemblyIdentity | None = None) -> dict[str, Any]:
    """Produce bounded, deterministic semantic evidence for a parsed fixture."""
    identity = assembly or AssemblyIdentity("", "")
    try:
        model = normalize_bom(rows, identity)
    except (TypeError, ValueError):
        return {"valid": False, "mapping_required": True, "assembly_identity": None}
    assembly_value = {"name": model.assembly[0], "revision": model.assembly[1]}
    if not any(model.assembly):
        assembly_value = None
    return {
        "valid": True,
        "result": "valid",
        "fingerprint": model.fingerprint,
        "assembly_identity": assembly_value,
        "mapping_required": assembly_value is None,
        "model_summary": {"component_count": len(model.components),
                          "material_component_count": len(model.components)},
    }


def _component(row: Mapping[str, Any]) -> Component:
    item = _norm(_first(row, ("item_number", "item", "part_number", "part number", "component")))
    revision = _norm(_first(row, ("revision", "component_revision", "component revision", "rev")))
    if not item:
        raise ValueError("each BOM row requires an item number")
    material = (
        ("category", _norm(_first(row, ("item_category", "category", "type")))),
        ("quantity", _number(_first(row, ("quantity", "qty", "amount")))),
        ("refdes", _refs(_first(row, ("reference_designator", "reference_designators", "refdes", "refs")))),
        ("install", _norm(_first(row, ("install", "installation", "install_type", "mount_type")))),
    )
    pairs: set[tuple[str, str]] = set()
    for index in range(1, 65):
        manufacturer = _first(row, (f"manufacturer_{index}", f"mfr_{index}", f"manufacturer {index}"))
        mpn = _first(row, (f"manufacturer_item_number_{index}", f"mpn_{index}", f"mpn {index}", f"part_number_{index}"))
        if _norm(manufacturer) or _norm(mpn):
            pairs.add((_norm(manufacturer), _norm(mpn)))
    # Also accept a single unnumbered pair used by common parsed-BOM adapters.
    manufacturer = _first(row, ("manufacturer", "mfr"))
    mpn = _first(row, ("manufacturer_item_number", "mpn", "manufacturer part number"))
    if _norm(manufacturer) or _norm(mpn):
        pairs.add((_norm(manufacturer), _norm(mpn)))
    optional = (("manufacturer_mpn", tuple(sorted(pairs))),)
    return Component((item, revision), material, optional)


def _safe(value: Any) -> Any:
    if isinstance(value, str) and len(value) > _MAX_VALUE_LENGTH:
        return value[:_MAX_VALUE_LENGTH] + "..."
    if isinstance(value, tuple):
        return [_safe(item) for item in value]
    return value


def normalize_bom(rows: Iterable[Mapping[str, Any]], assembly: AssemblyIdentity) -> BomSemantic:
    """Normalize rows and return a deterministic semantic BOM representation."""
    grouped: dict[tuple[str, str], list[Component]] = {}
    for row in rows:
        component = _component(row)
        grouped.setdefault(component.identity, []).append(component)
    components: list[Component] = []
    for identity in sorted(grouped):
        entries = grouped[identity]
        # Duplicate rows are retained as a deterministic multiset. This prevents
        # duplicate/refdes ambiguity from being silently discarded.
        components.extend(sorted(entries, key=lambda item: json.dumps(item.canonical(), sort_keys=True)))
    payload = {"assembly": assembly.normalized(), "components": [item.canonical() for item in components]}
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    return BomSemantic(assembly.normalized(), tuple(components), hashlib.sha256(encoded).hexdigest())


def _diff(left: BomSemantic, right: BomSemantic) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    if left.assembly != right.assembly:
        evidence.append({"kind": "assembly_revision", "left": _safe(left.assembly), "right": _safe(right.assembly)})
    left_by = {component.identity: component for component in left.components}
    right_by = {component.identity: component for component in right.components}
    for identity in sorted(set(left_by) | set(right_by)):
        a, b = left_by.get(identity), right_by.get(identity)
        if a is None or b is None:
            evidence.append({"kind": "component_added_or_missing", "identity": _safe(identity)})
            continue
        if a.material != b.material:
            evidence.append({"kind": "material_conflict", "identity": _safe(identity), "left": _safe(a.material), "right": _safe(b.material)})
    return evidence[:_MAX_EVIDENCE]


def compare_boms(left_rows: Iterable[Mapping[str, Any]], right_rows: Iterable[Mapping[str, Any]], *,
                 left_assembly: AssemblyIdentity, right_assembly: AssemblyIdentity) -> BomComparison:
    """Compare two parsed BOMs as equivalent, dominance, or conflict.

    Material facts (component identity, quantity, refdes, category, and install
    type) must match. MPN/manufacturer pairs are unordered sets: a strict superset
    is enrichment, including disjoint alternatives. Descriptions and source
    formatting are non-material. Assembly name and revision are explicit inputs;
    any revision mismatch conflicts.
    """
    left = normalize_bom(left_rows, left_assembly)
    right = normalize_bom(right_rows, right_assembly)
    conflicts = _diff(left, right)
    if conflicts:
        return BomComparison("conflict", left.fingerprint, right.fingerprint, tuple(conflicts))
    left_by = {component.identity: component for component in left.components}
    right_by = {component.identity: component for component in right.components}
    left_extra = right_extra = False
    for identity in left_by:
        a_pairs = dict(left_by[identity].optional).get("manufacturer_mpn", ())
        b_pairs = dict(right_by[identity].optional).get("manufacturer_mpn", ())
        left_extra |= not set(a_pairs) <= set(b_pairs)
        right_extra |= not set(b_pairs) <= set(a_pairs)
    if not left_extra and not right_extra:
        relation = "equivalent"
    elif right_extra or left_extra:
        # Alternatives are set-valued enrichment. Disjoint additions are not
        # contradictory; when both sides add alternatives there is no unique
        # richer side, so retain the same non-conflicting dominance relation.
        relation = "dominates"
    evidence = []
    if relation == "dominates":
        richer = "right" if right_extra and not left_extra else "left" if left_extra and not right_extra else "both"
        evidence.append({"kind": "optional_enrichment", "side": richer, "field": "manufacturer_mpn"})
    return BomComparison(relation, left.fingerprint, right.fingerprint, tuple(evidence[:_MAX_EVIDENCE]))
