"""Standalone opt-in author accounting; no dispatch, validation, or live I/O.

Inputs: trusted persisted package plans and the existing storage adapter. Outputs:
enroll -> membership; reserve -> reservation or hold; inspect/status -> projection.
Run tests: python -m pytest tests/test_author_budget.py (from AgentQuote).

Clock returns an aware datetime; token_factory must return a fresh unpredictable
owner token on EVERY call (default: secrets.token_hex). Never reuse a reservation
to dispatch twice. Worker validation does not reserve. All starts, including known
dispatch failures, consume a slot. Missing membership is explicitly legacy, not
permission to start. This module does not authorize callers or reconcile history.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
from datetime import datetime, timezone
from typing import Any, Callable
from uuid import UUID

from .core import BlobConflictError, BlobNotFoundError, ProcessingError

VERSION = "author-budget.v1"
LIMIT = 6
STALL_SECONDS = 1800
MAX_BYTES = 100_000
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_ROOT = "author-budget/v1"
_PHASES = {"author", "author-refinement"}


class BudgetError(ProcessingError):
    """Fail-closed contract/storage failure; contains no storage error details."""

    def __init__(self) -> None:
        super().__init__("budget_invalid")


def _require(condition: bool) -> None:
    if not condition:
        raise BudgetError()


def _identifier(value: Any) -> None:
    _require(isinstance(value, str) and _ID.fullmatch(value) is not None)


def _digest(value: Any) -> None:
    _require(isinstance(value, str) and _HASH.fullmatch(value) is not None)


def _bytes(value: Any) -> bytes:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    _require(len(raw) <= MAX_BYTES)
    return raw


def _hash(value: Any) -> str:
    return hashlib.sha256(_bytes(value)).hexdigest()


def _read(storage: Any, path: str) -> dict[str, Any] | None:
    try:
        raw = storage.download_registry(path, MAX_BYTES)
    except BlobNotFoundError:
        return None
    # None, malformed JSON, oversize bytes, and all access errors are NOT absence.
    _require(isinstance(raw, bytes) and len(raw) <= MAX_BYTES)
    value = json.loads(raw.decode("utf-8"))
    _require(isinstance(value, dict))
    return value


def _put(storage: Any, path: str, value: dict[str, Any]) -> None:
    try:
        storage.upload_registry_immutable(path, _bytes(value), "application/json")
    except BlobConflictError:
        _require(_read(storage, path) == value)


def _now(clock: Callable[[], datetime]) -> float:
    value = clock()
    _require(isinstance(value, datetime) and value.tzinfo is not None)
    result = value.timestamp()
    _require(math.isfinite(result))
    return result


def _clock() -> datetime:
    return datetime.now(timezone.utc)


def _token() -> str:
    return secrets.token_hex(32)


def _member_path(request_id: str) -> str:
    return f"{_ROOT}/members/{request_id}.json"


def require_budget(storage: Any, request_id: str) -> None:
    """Publish fail-closed intent before a new request becomes observable."""
    _identifier(request_id)
    _put(storage, f"{_ROOT}/required/{request_id}.json", {"version": VERSION, "request_id": request_id})


def begin_execution(storage: Any, group_id: str, slot: int, request_id: str,
                    reservation_hash: str) -> str:
    """Single-use worker ownership, acquired before constructing the model."""
    claim = _reservation(storage, group_id, slot, request_id, reservation_hash)
    _require(_read(storage, _slot_path(group_id, slot, "terminal")) is None
             and _read(storage, _slot_path(group_id, slot, "unknown")) is None)
    token = _token()
    record = {"version": VERSION, "request_id": request_id,
              "reservation_hash": claim["reservation_hash"], "execution_owner": token}
    record["version"] = claim["version"]
    # Any conflict loses execution ownership, including identical-byte conflicts.
    storage.upload_registry_immutable(_slot_path(group_id, slot, "execution"), _bytes(record), "application/json")
    return token


def _policy_path(group_id: str) -> str:
    return f"{_ROOT}/groups/{group_id}/policy.json"


def _slot_path(group_id: str, slot: int, kind: str) -> str:
    return f"{_ROOT}/groups/{group_id}/slots/{slot}.{kind}.json"


def _sources(value: Any) -> list[dict[str, Any]]:
    _require(isinstance(value, list) and 0 < len(value) <= 1000)
    result = []
    for item in value:
        _require(isinstance(item, dict))
        source = {key: item.get(key) for key in
                  ("document_id", "blob_path", "size_bytes", "sha256")}
        _identifier(source["document_id"])
        _digest(source["sha256"])
        _require(type(source["size_bytes"]) is int and source["size_bytes"] > 0)
        path = source["blob_path"]
        _require(isinstance(path, str) and 0 < len(path) <= 2048
                 and "\\" not in path and ".." not in path.split("/"))
        result.append(source)
    _require(len({item["document_id"] for item in result}) == len(result))
    return sorted(result, key=lambda item: item["document_id"])


def _identity(plan: dict[str, Any], profile: str) -> dict[str, Any]:
    for key in ("customer_key", "parser_key", "package_version"):
        _identifier(plan.get(key))
    _digest(profile)
    rfq = plan.get("rfq_uuid")
    _require(isinstance(rfq, str) and str(UUID(rfq)) == rfq.lower())
    return {"version": VERSION, "namespace": _ROOT,
            "customer_key": plan["customer_key"], "parser_key": plan["parser_key"],
            "rfq_uuid": rfq.lower(), "package_version": plan["package_version"],
            "profile_hash": profile}


def _policy_valid(policy: Any, group_id: str) -> None:
    _require(isinstance(policy, dict))
    identity = policy.get("identity")
    _require(isinstance(identity, dict))
    _require(identity == _identity(identity, identity.get("profile_hash")))
    _require(_hash(identity) == group_id and policy.get("group_id") == group_id
             and policy.get("version") == VERSION
             and type(policy.get("limit")) is int and policy["limit"] == LIMIT
             and type(policy.get("stall_seconds")) is int
             and policy["stall_seconds"] == STALL_SECONDS)
    _require(policy.get("sources") == _sources(policy.get("sources")))
    if "package_choice" in policy:
        _require(policy["package_choice"] == _choice_identity(policy["package_choice"]))


def _choice_identity(reference: Any) -> dict[str, str]:
    _require(isinstance(reference, dict))
    result = {key: reference.get(key) for key in ("request_id", "result_sha256")}
    for value in result.values():
        _digest(value)
    return result


def _check_sources(storage: Any, policy: dict[str, Any], evidence: list[dict[str, Any]],
                   plan: dict[str, Any]) -> None:
    """Added files must be reviewed reopens of this group's pinned package choice.

    The first policy and all claims stay immutable. The new membership binds its
    own persisted plan; later readers recheck that plan and the choice result.
    """
    extra = [item for item in evidence if item not in policy["sources"]]
    if not extra:
        return
    from .bom_package_choice import bound_state, PackageChoiceService
    _require(policy.get("package_choice") == _choice_identity(plan.get("package_choice")))
    state = bound_state(storage, plan["package_choice"])
    request = PackageChoiceService(storage).request(state["request_id"])
    identity = policy["identity"]
    _require(all(request.get(key) == identity[key]
                 for key in ("customer_key", "rfq_uuid", "package_version")))
    originals = _sources(request["documents"])
    reopened = set(state.get("reopened_document_ids", []))
    pinned = {item["document_id"]: item for item in policy["sources"]}
    _require(all(item["document_id"] not in pinned
                 and item["document_id"] in reopened and item in originals for item in extra))


def _membership(storage: Any, request_id: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
    _identifier(request_id)
    member = _read(storage, _member_path(request_id))
    if member is None:
        return None
    group_id = member.get("group_id")
    _digest(group_id)
    policy = _read(storage, _policy_path(group_id))
    _policy_valid(policy, group_id)
    _require(member.get("version") == VERSION and member.get("request_id") == request_id
             and member.get("policy_hash") == _hash(policy))
    _digest(member.get("plan_id"))
    evidence = _sources(member.get("sources"))
    _require(evidence == member["sources"])
    if any(item not in policy["sources"] for item in evidence):
        from .package_resolution import ParentPackageService
        plan = ParentPackageService(storage)._plan(member["plan_id"])
        _require(_identity(plan, policy["identity"]["profile_hash"]) == policy["identity"])
        _require(all(item in _sources(plan["sources"]) for item in evidence))
        assignments = [item for item in plan["assignments"]
                       if item.get("kind") == "candidate_request" and item.get("request_id") == request_id]
        _require(len(assignments) == 1
                 and set(assignments[0]["document_ids"]) == {item["document_id"] for item in evidence}
                 and assignments[0]["profile_hash"] == policy["identity"]["profile_hash"])
        _check_sources(storage, policy, evidence, plan)
    return member, policy


def enroll(storage: Any, plan: dict[str, Any], request_id: str, *,
           clock: Callable[[], datetime] = _clock,
           token_factory: Callable[[], str] = _token) -> dict[str, Any]:
    """Publish trusted opt-in membership, never backfill existing author history.

    First enrollment pins all same-profile source evidence in the supplied plan.
    Later plans share the group; only verified same-choice reopens can add files.
    Enrollment itself consumes no slots; clock/token are reserved API seams.
    Re-enrolling identical membership is idempotent even after authoring starts.
    """
    try:
        from .package_resolution import ParentPackageService
        _identifier(request_id)
        _require(isinstance(plan, dict) and plan.get("version") == "package-plan.v1")
        plan_id = plan.get("plan_id")
        _digest(plan_id)
        _require(_hash({key: value for key, value in plan.items() if key != "plan_id"}) == plan_id)
        _require(_read(storage, ParentPackageService._plan_path(plan_id)) == plan)
        inventory = _sources(plan.get("sources"))
        assignments = plan.get("assignments")
        _require(isinstance(assignments, list) and 0 < len(assignments) <= 1000)
        target = [item for item in assignments if isinstance(item, dict)
                  and item.get("kind") == "candidate_request"
                  and item.get("request_id") == request_id]
        _require(len(target) == 1)
        profile = target[0].get("profile_hash")
        identity = _identity(plan, profile)
        group_id = _hash(identity)
        group_sources = []
        target_sources = []
        seen_requests = set()
        for assignment in assignments:
            _require(isinstance(assignment, dict))
            if assignment.get("kind") != "candidate_request" or assignment.get("profile_hash") != profile:
                continue
            child_id = assignment.get("request_id")
            _identifier(child_id)
            _require(child_id not in seen_requests)
            seen_requests.add(child_id)
            documents = assignment.get("document_ids")
            _require(isinstance(documents, list) and documents and len(set(documents)) == len(documents))
            evidence = [item for item in inventory if item["document_id"] in documents]
            _require(len(evidence) == len(documents))
            child = _read(storage, f"candidates/requests/{child_id}.json")
            _require(isinstance(child, dict) and child.get("request_id") == child_id
                     and child.get("customer_key") == identity["customer_key"]
                     and child.get("parser_key") == identity["parser_key"]
                     and child.get("profile_hash") == profile
                     and _sources(child.get("source_evidence")) == evidence)
            group_sources.extend(evidence)
            if child_id == request_id:
                target_sources = evidence
        group_sources = _sources(group_sources)  # Reject overlapping assignments.
        policy = _read(storage, _policy_path(group_id))
        if policy is None:
            policy = {"version": VERSION, "group_id": group_id, "identity": identity,
                      "limit": LIMIT, "stall_seconds": STALL_SECONDS, "sources": group_sources}
            if plan.get("package_choice") is not None:
                policy["package_choice"] = _choice_identity(plan["package_choice"])
        _policy_valid(policy, group_id)
        _check_sources(storage, policy, group_sources, plan)
        member = {"version": VERSION, "request_id": request_id, "group_id": group_id,
                  "policy_hash": _hash(policy), "plan_id": plan_id, "sources": target_sources}
        existing = _membership(storage, request_id)
        if existing is not None:
            # Equivalent trusted parent plans may differ in request ordering or
            # job identity. Never rewrite first enrollment or reset its slots.
            _require({key: value for key, value in existing[0].items() if key != "plan_id"}
                     == {key: value for key, value in member.items() if key != "plan_id"})
            return existing[0]
        # Listings are bounded and must succeed. Own request.json is not history.
        root = f"candidates/{request_id}/"
        candidates = storage.list_candidate_paths(root, 100)
        automation = storage.list_registry_paths(f"candidate-automation/{request_id}/", 100)
        _require(isinstance(candidates, list) and isinstance(automation, list)
                 and not automation and not [path for path in candidates if path != root + "request.json"])
        _put(storage, _policy_path(group_id), policy)
        _put(storage, _member_path(request_id), member)
        return member
    except Exception as exc:
        raise BudgetError() from exc


def _reservation(storage: Any, group_id: str, slot: int, request_id: str,
                 reservation_hash: str) -> dict[str, Any]:
    _digest(group_id)
    _identifier(request_id)
    _digest(reservation_hash)
    _require(type(slot) is int and 1 <= slot <= LIMIT)
    binding = _membership(storage, request_id)
    _require(binding is not None and binding[0]["group_id"] == group_id)
    claim = _read(storage, _slot_path(group_id, slot, "claim"))
    _require(isinstance(claim, dict))
    body = {key: value for key, value in claim.items() if key != "reservation_hash"}
    _require(claim.get("reservation_hash") == reservation_hash == _hash(body)
             and claim.get("version") in {VERSION, "author-budget-recovery.v1"} and claim.get("group_id") == group_id
             and claim.get("slot") == slot and type(claim.get("slot")) is int
             and claim.get("request_id") == request_id
             and claim.get("membership_hash") == _hash(binding[0]))
    _identifier(claim.get("owner_token"))
    _require(claim.get("phase") in _PHASES)
    if claim["phase"] == "author-refinement":
        _identifier(claim.get("source_candidate"))
        _digest(claim.get("failure_hash"))
    else:
        _require(claim.get("source_candidate") is None and claim.get("failure_hash") is None)
    _require(type(claim.get("claimed_at")) in (int, float) and math.isfinite(claim["claimed_at"]))
    if claim["version"] == "author-budget-recovery.v1":
        from .author_recovery import validate_recovery_claim
        validate_recovery_claim(storage, claim)
    return claim


def complete(storage: Any, group_id: str, slot: int, request_id: str,
             reservation_hash: str, state: str, candidate_id: str | None = None,
             *, execution_owner: str | None = None) -> dict[str, Any]:
    """Worker terminal publication; verifies membership and immutable claim first.

    state is completed (requires candidate_id) or failed (known terminal failure,
    including a known dispatch rejection). Unknown starts are never cleared.
    """
    try:
        claim = _reservation(storage, group_id, slot, request_id, reservation_hash)
        owner = _read(storage, _slot_path(group_id, slot, "execution"))
        _require(owner is not None and execution_owner is not None
                 and owner.get("execution_owner") == execution_owner
                 and owner.get("version") == claim["version"] and owner.get("request_id") == request_id
                 and owner.get("reservation_hash") == reservation_hash)
        _require(state in {"completed", "failed"})
        if state == "completed":
            _identifier(candidate_id)
        else:
            _require(candidate_id is None)
        record = {"version": claim["version"], "request_id": request_id,
                  "reservation_hash": reservation_hash, "state": state, "candidate_id": candidate_id,
                  "execution_owner": execution_owner}
        _put(storage, _slot_path(group_id, slot, "terminal"), record)
        return record
    except Exception as exc:
        raise BudgetError() from exc


class Budget:
    """Fixed six-slot accounting; all read failures become budget_invalid holds.

    reserve returns status=reserved plus immutable claim fields, or status=hold
    with reason=budget_{exhausted,in_flight,stalled,start_unknown,no_progress,invalid}.
    inspect/status returns status=available/hold/legacy, reason, used, and group_id.
    Stalled begins at age >= 1800 seconds; neither stalled nor unknown is recoverable
    here. Only a newly returned reservation permits one caller-owned dispatch.
    """

    def __init__(self, storage: Any, request_id: str, *,
                 clock: Callable[[], datetime] = _clock,
                 token_factory: Callable[[], str] = _token):
        self.storage, self.request_id = storage, request_id
        self.clock, self.token_factory = clock, token_factory

    @staticmethod
    def _hold(reason: str, used: int = 0, group_id: str | None = None) -> dict[str, Any]:
        return {"status": "hold", "reason": reason, "used": used, "group_id": group_id}

    def _inspect(self, snapshot: list[tuple[dict[str, Any], dict[str, Any] | None]] | None = None) -> dict[str, Any]:
        binding = _membership(self.storage, self.request_id)
        if binding is None:
            if _read(self.storage, f"{_ROOT}/required/{self.request_id}.json") is not None:
                return self._hold("budget_invalid")
            return {"status": "legacy", "reason": None, "used": 0, "group_id": None}
        group_id = binding[0]["group_id"]
        now = _now(self.clock)
        used, vacant = 0, False
        holds = set()
        candidate_ids = set()
        for slot in range(1, LIMIT + 1):
            claim = _read(self.storage, _slot_path(group_id, slot, "claim"))
            markers = {kind: _read(self.storage, _slot_path(group_id, slot, kind))
                       for kind in ("started", "unknown", "terminal", "dispatch", "execution")}
            if claim is None:
                _require(not any(item is not None for item in markers.values()))
                vacant = True
                continue
            _require(not vacant)
            claim = _reservation(self.storage, group_id, slot, claim.get("request_id"),
                                 claim.get("reservation_hash"))
            used += 1
            for kind, marker in markers.items():
                if marker is None:
                    continue
                _require(marker.get("version") == claim["version"]
                         and marker.get("request_id") == claim["request_id"]
                         and marker.get("reservation_hash") == claim["reservation_hash"])
                if kind == "started":
                    _identifier(marker.get("execution"))
                    _require(marker.get("state") == "started")
                elif kind == "unknown":
                    _require(marker.get("state") == "unknown")
                elif kind == "dispatch":
                    _require(marker.get("state") == "dispatch")
                elif kind == "execution":
                    _identifier(marker.get("execution_owner"))
                else:
                    _require(markers["execution"] is not None
                             and marker.get("execution_owner") == markers["execution"].get("execution_owner"))
                    _require(marker.get("state") in {"completed", "failed"})
                    candidate = marker.get("candidate_id")
                    if marker["state"] == "completed":
                        _identifier(candidate)
                        if candidate == claim["source_candidate"] or candidate in candidate_ids:
                            holds.add("budget_no_progress")
                        candidate_ids.add(candidate)
                    else:
                        _require(candidate is None)
            _require(claim["claimed_at"] <= now)
            if snapshot is not None:
                snapshot.append((claim, markers["terminal"]))
            dispatch_pending = (markers["dispatch"] is not None
                                and markers["started"] is None)
            if markers["unknown"] is not None or dispatch_pending:
                holds.add("budget_start_unknown")
            elif markers["terminal"] is None:
                holds.add("budget_stalled" if now - claim["claimed_at"] >= STALL_SECONDS
                          else "budget_in_flight")
        for reason in ("budget_start_unknown", "budget_no_progress", "budget_stalled", "budget_in_flight"):
            if reason in holds:
                return self._hold(reason, used, group_id)
        if used == LIMIT:
            return self._hold("budget_exhausted", used, group_id)
        return {"status": "available", "reason": None, "used": used, "group_id": group_id}

    def inspect(self) -> dict[str, Any]:
        try:
            return self._inspect()
        except Exception:
            return self._hold("budget_invalid")

    def status(self) -> dict[str, Any]:
        return self.inspect()

    def selected_candidate(self) -> str | None:
        """Only completed budget reservations establish successor lineage."""
        binding = _membership(self.storage, self.request_id)
        if binding is None:
            return None
        selected = None
        self._inspect()  # Verify all marker bindings before selecting lineage.
        for slot in range(1, LIMIT + 1):
            claim = _read(self.storage, _slot_path(binding[0]["group_id"], slot, "claim"))
            terminal = _read(self.storage, _slot_path(binding[0]["group_id"], slot, "terminal"))
            if (claim is not None and claim["request_id"] == self.request_id
                    and terminal is not None and terminal["state"] == "completed"):
                selected = terminal["candidate_id"]
        return selected

    def failed_tries(self, phase: str, source_candidate: str | None, failure_hash: str | None) -> int:
        """Earlier tries of this exact fix that ended failed without a parser (round 12)."""
        binding = _membership(self.storage, self.request_id)
        if binding is None:
            return 0
        count = 0
        for slot in range(1, LIMIT + 1):
            claim = _read(self.storage, _slot_path(binding[0]["group_id"], slot, "claim"))
            terminal = _read(self.storage, _slot_path(binding[0]["group_id"], slot, "terminal"))
            if (claim is not None and claim.get("request_id") == self.request_id and claim.get("phase") == phase
                    and claim.get("source_candidate") == source_candidate and claim.get("failure_hash") == failure_hash
                    and terminal is not None and terminal.get("state") == "failed"):
                count += 1
        return count

    def reserve(self, phase: str, source_candidate: str | None = None,
                failure_hash: str | None = None) -> dict[str, Any]:
        try:
            _require(phase in _PHASES)
            if phase == "author-refinement":
                _identifier(source_candidate)
                _digest(failure_hash)
            else:
                _require(source_candidate is None and failure_hash is None)
            # Eligibility and next-slot selection use the SAME validated scan.
            # A concurrent append must claim that same next slot; we lose the
            # immutable conflict, never rescan/advance without rechecking.
            snapshot: list[tuple[dict[str, Any], dict[str, Any] | None]] = []
            projection = self._inspect(snapshot)
            if projection["status"] == "hold":
                return projection
            if projection["status"] != "available":
                return self._hold("budget_invalid")
            selected = None
            repeated_intent = False
            for prior, terminal in snapshot:
                if prior["request_id"] != self.request_id:
                    continue
                if terminal is not None and terminal["state"] == "completed":
                    selected = terminal["candidate_id"]
                # A repeat of the same fix is refused, unless the earlier try died before producing a
                # parser (refused code, failed model call): that try is counted and may be retried
                # within the limit (round 12, A044 stalled for 8 hours on one refused rewrite).
                repeated_intent |= (phase == "author-refinement" and prior["phase"] == phase
                                    and prior["source_candidate"] == source_candidate
                                    and prior["failure_hash"] == failure_hash
                                    and (terminal is None or terminal["state"] != "failed"))
            if ((phase == "author" and selected is not None)
                    or (phase == "author-refinement" and selected != source_candidate)
                    or repeated_intent):
                return self._hold("budget_no_progress", projection["used"], projection["group_id"])
            group_id, slot = projection["group_id"], projection["used"] + 1
            binding = _membership(self.storage, self.request_id)
            _require(binding is not None)
            token = self.token_factory()
            _identifier(token)
            body = {"version": VERSION, "group_id": group_id, "slot": slot,
                    "request_id": self.request_id, "membership_hash": _hash(binding[0]),
                    "phase": phase, "source_candidate": source_candidate, "failure_hash": failure_hash,
                    "claimed_at": _now(self.clock), "owner_token": token}
            claim = {**body, "reservation_hash": _hash(body)}
            # Unlike idempotent metadata writes, ANY conflict loses this claim.
            # Random owner bytes prevent identical-payload success in the adapter.
            try:
                self.storage.upload_registry_immutable(_slot_path(group_id, slot, "claim"),
                                                       _bytes(claim), "application/json")
            except BlobConflictError:
                return self._hold("budget_in_flight", slot, group_id)
            _require(_read(self.storage, _slot_path(group_id, slot, "claim")) == claim)
            return {"status": "reserved", **claim}
        except Exception:
            return self._hold("budget_invalid")

    def _mark(self, reservation: dict[str, Any], state: str,
              execution: str | None = None) -> dict[str, Any]:
        try:
            _require(isinstance(reservation, dict) and reservation.get("request_id") == self.request_id)
            claim = _reservation(self.storage, reservation.get("group_id"), reservation.get("slot"),
                                 self.request_id, reservation.get("reservation_hash"))
            _require(reservation == {"status": "reserved", **claim})
            if state == "started":
                _identifier(execution)
            record = {"version": claim["version"], "request_id": self.request_id,
                      "reservation_hash": claim["reservation_hash"], "state": state}
            if execution is not None:
                record["execution"] = execution
            _put(self.storage, _slot_path(claim["group_id"], claim["slot"], state), record)
            return record
        except Exception as exc:
            raise BudgetError() from exc

    def mark_started(self, reservation: dict[str, Any], execution: str) -> dict[str, Any]:
        return self._mark(reservation, "started", execution)

    def mark_dispatch(self, reservation: dict[str, Any]) -> dict[str, Any]:
        """Durable intent must precede management I/O, never best effort."""
        return self._mark(reservation, "dispatch")

    def mark_unknown(self, reservation: dict[str, Any]) -> dict[str, Any]:
        return self._mark(reservation, "unknown")