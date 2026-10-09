"""Immutable, deterministic parser candidate requests."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from .master_registry import RegistryError, StructuralProfile, _hash

CANDIDATE_VERSION = "candidate-request.v2"


def unique_fixtures(source_evidence: tuple[dict[str, Any], ...] | list[dict[str, Any]]) -> tuple[dict[str, Any], ...]:
    """Collapse exact-byte duplicates while preserving deterministic aliases."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for item in source_evidence:
        groups.setdefault(str(item.get("sha256", "")).lower(), []).append(dict(item))
    fixtures = []
    for digest in sorted(groups):
        aliases = sorted(groups[digest], key=lambda item: (str(item.get("document_id", "")), str(item.get("blob_path", ""))))
        fixture = dict(aliases[0])
        fixture["source_aliases"] = [{"document_id": item.get("document_id"), "blob_path": item.get("blob_path")} for item in aliases]
        fixture["alias_count"] = len(aliases)
        fixtures.append(fixture)
    return tuple(fixtures)


class CandidateRequiredError(Exception):
    """Safe signal that an unrecognized format needs an approved candidate."""
    def __init__(self, request_id: str, parser_key: str, profile_hash: str,
                 document_ids: tuple[str, ...] = (), state: str = "candidate_required"):
        self.request_id = request_id
        self.parser_key = parser_key
        self.profile_hash = profile_hash
        self.document_ids = document_ids
        self.state = state
        super().__init__("candidate required")


class AggregateCandidateRequiredError(Exception):
    """Several independent source profiles need candidates before processing."""
    def __init__(self, candidates: tuple[CandidateRequiredError, ...], plan_id: str | None = None):
        self.candidates = candidates
        self.plan_id = plan_id
        super().__init__("candidates required")


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
    # Completed package choice that selected these sources (bom_package_choice); None before 2026-10-08.
    package_choice: dict[str, str] | None = None

    @classmethod
    def build(cls, parser_key: str, customer_key: str, profile: StructuralProfile,
              source_evidence: tuple[dict[str, Any], ...], created_at: str,
              *, stable_order: bool = False,
              package_choice: dict[str, str] | None = None) -> "CandidateRequest":
        if stable_order:
            source_evidence = tuple(sorted(source_evidence, key=lambda item: (item["document_id"], item["blob_path"])))
        identity = {"version": CANDIDATE_VERSION, "parser_key": parser_key,
                    "customer_key": customer_key, "profile_hash": profile.profile_hash,
                    "source_evidence": list(source_evidence)}
        if package_choice is not None:
            # Part of the identity only when present: earlier request ids are unchanged.
            identity["package_choice"] = dict(package_choice)
        request_id = _hash(identity)
        return cls(request_id, parser_key, customer_key, profile.profile_hash,
                   source_evidence, profile.to_dict(), "requested", created_at,
                   package_choice=None if package_choice is None else dict(package_choice))

    def to_dict(self) -> dict[str, Any]:
        value = {"version": self.version, "request_id": self.request_id,
                 "parser_key": self.parser_key, "customer_key": self.customer_key,
                 "profile_hash": self.profile_hash, "source_evidence": list(self.source_evidence),
                 "profile": self.profile, "status": self.status, "created_at": self.created_at}
        if self.package_choice is not None:
            value["package_choice"] = dict(self.package_choice)
        return value


def request_path(request_id: str) -> str:
    return f"candidates/requests/{request_id}.json"


def persist_candidate(storage: Any, parser_key: str, customer_key: str,
                      profile: StructuralProfile, source_evidence: tuple[dict[str, Any], ...],
                      clock: Callable[[], datetime] | None = None, *, stable_order: bool = False,
                      package_choice: dict[str, str] | None = None) -> CandidateRequest:
    if stable_order:
        source_evidence = tuple(sorted(source_evidence, key=lambda item: (item["document_id"], item["blob_path"])))
    now = (clock or (lambda: datetime.now(timezone.utc)))()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    request = CandidateRequest.build(parser_key, customer_key, profile, source_evidence,
                                     now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                                     package_choice=package_choice)
    import json
    data = json.dumps(request.to_dict(), sort_keys=True, separators=(",", ":")).encode()
    path = request_path(request.request_id)
    if stable_order:
        from .core import BlobNotFoundError
        try:
            storage.download_registry(path, 1_000_000)
        except BlobNotFoundError:
            from .author_budget import require_budget
            require_budget(storage, request.request_id)
    try:
        storage.upload_registry_immutable(path, data, "application/json")
    except Exception as exc:
        # Storage adapters report immutable conflicts as BlobConflictError. Read
        # and validate the existing record so equivalent retries remain idempotent.
        try:
            existing = json.loads(storage.download_registry(path, len(data) + 1))
        except Exception:
            raise
        if (not isinstance(existing, dict) or existing.get("version") not in {CANDIDATE_VERSION, "candidate-request.v1"}
                or existing.get("request_id") != request.request_id
                or existing.get("parser_key") != parser_key
                or existing.get("customer_key") != customer_key
                or existing.get("profile_hash") != profile.profile_hash
                or existing.get("source_evidence") != list(source_evidence)
                or existing.get("package_choice") != package_choice):
            raise RegistryError("CANDIDATE_CONFLICT") from exc
        return CandidateRequest(existing["request_id"], existing["parser_key"], existing["customer_key"],
                                existing["profile_hash"], tuple(existing.get("source_evidence", ())),
                                existing.get("profile", {}), existing.get("status", "requested"),
                                existing.get("created_at", ""), existing.get("version", CANDIDATE_VERSION),
                                existing.get("package_choice"))
    return request
