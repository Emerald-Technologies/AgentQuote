"""Read-only Blob master parser registry validation and selection."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from .master_registry import (ApprovalRecord, ApprovedVariant, RegistryError,
                              VariantSignature, _hash, profile_bytes)


@dataclass(frozen=True)
class RegistrySelection:
    variant: ApprovedVariant
    code: bytes
    generation: int
    selection_mode: str = "registry"


class BlobRegistryReader:
    """Validate a mutable master index and immutable approved parser blobs.

    ``read`` receives a relative blob path and a byte limit.  This keeps the
    registry usable with local fakes as well as Azure adapters.
    """
    def __init__(self, read: Callable[[str, int], bytes], *, max_bytes: int = 1_000_000):
        self.read = read
        self.max_bytes = max_bytes

    def _json(self, path: str) -> dict[str, Any]:
        try:
            value = json.loads(self.read(path, self.max_bytes).decode("utf-8"))
        except (FileNotFoundError,) :
            raise
        except Exception as exc:
            if type(exc).__name__ in {"BlobNotFoundError", "ResourceNotFoundError"}:
                raise FileNotFoundError(path) from exc
            raise RegistryError("REGISTRY_CORRUPT") from exc
        if not isinstance(value, dict):
            raise RegistryError("REGISTRY_CORRUPT")
        return value

    def select(self, parser_key: str, customer: str, data: bytes, extension: str) -> RegistrySelection:
        try:
            master = self._json(f"registry/{parser_key}/master.json")
        except FileNotFoundError:
            raise RegistryError("REGISTRY_ABSENT")
        if master.get("schema") != "master-registry.v1" or master.get("parser_key") != parser_key:
            raise RegistryError("REGISTRY_CORRUPT")
        generation = master.get("generation")
        variants = master.get("variants")
        if type(generation) is not int or generation < 0 or not isinstance(variants, list):
            raise RegistryError("REGISTRY_CORRUPT")
        found: list[tuple[ApprovedVariant, VariantSignature]] = []
        try:
            for item in variants:
                if not isinstance(item, dict): raise ValueError()
                variant = ApprovedVariant(parser_key=parser_key, variant_id=item["variant_id"],
                    version=item["version"], code_path=item["code_path"], code_sha256=item["code_sha256"],
                    signature_hash=item["signature_hash"], rule_hash=item["rule_hash"], approval_id=item["approval_id"])
                if variant.code_path != f"approved/{parser_key}/{variant.variant_id}/{variant.version}/parse.py": raise ValueError()
                signature = VariantSignature.from_dict(self._json(f"approved/{parser_key}/{variant.variant_id}/{variant.version}/signature.json"))
                if signature.rule_hash != variant.rule_hash:
                    raise ValueError()
                approval_path = f"approved/{parser_key}/{variant.variant_id}/{variant.version}/approval.json"
                if item.get("signature_path") not in (None, f"approved/{parser_key}/{variant.variant_id}/{variant.version}/signature.json"):
                    raise ValueError()
                if item.get("approval_path") not in (None, approval_path):
                    raise ValueError()
                approval_data = self._json(approval_path)
                supplied_approval_hash = approval_data.pop("approval_hash", None)
                approval = ApprovalRecord(**approval_data)
                if approval.approval_hash != supplied_approval_hash:
                    raise ValueError()
                if (approval.parser_key != parser_key or approval.variant_id != variant.variant_id
                        or approval.version != variant.version or approval.approval_id != variant.approval_id
                        or approval.customer != customer
                        or approval.signature_hash != variant.signature_hash or approval.rule_hash != variant.rule_hash
                        or approval.code_sha256 != variant.code_sha256): raise ValueError()
                found.append((variant, signature))
        except RegistryError:
            raise
        except Exception as exc:
            raise RegistryError("REGISTRY_CORRUPT") from exc
        profile = profile_bytes(data, extension)
        matches = [(variant, sig) for variant, sig in found if sig.matches(profile)]
        if len(matches) == 0: raise RegistryError("PARSER_FORMAT_NOT_RECOGNIZED", {"parser_key": parser_key, "match_count": 0})
        if len(matches) > 1: raise RegistryError("PARSER_FORMAT_AMBIGUOUS", {"parser_key": parser_key, "match_count": len(matches)})
        variant, _ = matches[0]
        code = self.read(variant.code_path, self.max_bytes)
        if _hash(code) != variant.code_sha256:
            raise RegistryError("REGISTRY_CORRUPT")
        return RegistrySelection(variant, code, generation)
