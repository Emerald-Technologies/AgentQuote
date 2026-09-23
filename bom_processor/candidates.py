"""Immutable, deterministic parser candidate requests."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from .master_registry import RegistryError, StructuralProfile, _hash

CANDIDATE_VERSION = "candidate-request.v1"


class CandidateRequiredError(Exception):
    """Safe signal that an unrecognized format needs an approved candidate."""
    def __init__(self, request_id: str, parser_key: str, profile_hash: str):
        self.request_id = request_id
        self.parser_key = parser_key
        self.profile_hash = profile_hash
        super().__init__("candidate required")


@dataclass(frozen=True)
class CandidateRequest:
    request_id: str
    parser_key: str
    customer_key: str
    profile_hash: str
    source_evidence: tuple[dict[str, Any], ...]
    profile: dict[str, Any]
    status: str
    created_at: str
    version: str = CANDIDATE_VERSION

    @classmethod
    def build(cls, parser_key: str, customer_key: str, profile: StructuralProfile,
              source_evidence: tuple[dict[str, Any], ...], created_at: str) -> "CandidateRequest":
        request_id = _hash({"version": CANDIDATE_VERSION, "parser_key": parser_key,
                            "customer_key": customer_key, "profile_hash": profile.profile_hash})
        return cls(request_id, parser_key, customer_key, profile.profile_hash,
                   source_evidence, profile.to_dict(), "requested", created_at)

    def to_dict(self) -> dict[str, Any]:
        return {"version": self.version, "request_id": self.request_id,
                "parser_key": self.parser_key, "customer_key": self.customer_key,
                "profile_hash": self.profile_hash, "source_evidence": list(self.source_evidence),
                "profile": self.profile, "status": self.status, "created_at": self.created_at}


def request_path(request_id: str) -> str:
    return f"candidates/requests/{request_id}.json"


def persist_candidate(storage: Any, parser_key: str, customer_key: str,
                      profile: StructuralProfile, source_evidence: tuple[dict[str, Any], ...],
                      clock: Callable[[], datetime] | None = None) -> CandidateRequest:
    now = (clock or (lambda: datetime.now(timezone.utc)))()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    request = CandidateRequest.build(parser_key, customer_key, profile, source_evidence,
                                     now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"))
    import json
    data = json.dumps(request.to_dict(), sort_keys=True, separators=(",", ":")).encode()
    path = request_path(request.request_id)
    try:
        storage.upload_registry_immutable(path, data, "application/json")
    except Exception as exc:
        # Storage adapters report immutable conflicts as BlobConflictError. Read
        # and validate the existing record so equivalent retries remain idempotent.
        try:
            existing = json.loads(storage.download_registry(path, len(data) + 1))
        except Exception:
            raise
        if (not isinstance(existing, dict) or existing.get("version") != CANDIDATE_VERSION
                or existing.get("request_id") != request.request_id
                or existing.get("parser_key") != parser_key
                or existing.get("customer_key") != customer_key
                or existing.get("profile_hash") != profile.profile_hash):
            raise RegistryError("CANDIDATE_CONFLICT") from exc
        return CandidateRequest(existing["request_id"], existing["parser_key"], existing["customer_key"],
                                existing["profile_hash"], tuple(existing.get("source_evidence", ())),
                                existing.get("profile", {}), existing.get("status", "requested"),
                                existing.get("created_at", ""), existing.get("version", CANDIDATE_VERSION))
    return request
