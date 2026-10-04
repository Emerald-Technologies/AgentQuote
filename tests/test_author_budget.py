"""Offline author-budget contracts, including create-if-absent races.

Run from AgentQuote: python -m pytest tests/test_author_budget.py.
Inputs: fake persisted plans/storage. Outputs: assertions; no dispatch or network.
"""
import copy
import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from bom_processor.author_budget import Budget, BudgetError, complete, enroll
from bom_processor.core import BlobConflictError, BlobNotFoundError

RFQ = "12345678-1234-4234-8234-123456789012"
PROFILE = "a" * 64
NOW = datetime(2026, 10, 4, tzinfo=timezone.utc)


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


class Storage:
    """Match adapter idempotency: identical immutable bytes do NOT conflict."""

    def __init__(self):
        self.registry = {}
        self.candidates = {}
        self.lock = threading.Lock()
        self.read_error = None
        self.write_error = None
        self.list_error = None
        self.race = None
        self.claim_payloads = []
        self.read_limits = []

    def download_registry(self, path, max_bytes):
        self.read_limits.append(max_bytes)
        if self.read_error and self.read_error[0] in path:
            raise self.read_error[1]
        with self.lock:
            if path not in self.registry:
                raise BlobNotFoundError()
            data = self.registry[path]
        if len(data) > max_bytes:
            raise ValueError("too large")
        return data

    def upload_registry_immutable(self, path, data, content_type):
        assert content_type == "application/json"
        if self.write_error:
            raise self.write_error
        if path.endswith(".claim.json"):
            self.claim_payloads.append(data)
            if self.race:
                self.race.wait(timeout=5)
        with self.lock:
            if path in self.registry and self.registry[path] != data:
                raise BlobConflictError()
            self.registry[path] = data

    def list_candidate_paths(self, prefix, max_results):
        return self._paths(self.candidates, prefix, max_results)

    def list_registry_paths(self, prefix, max_results):
        return self._paths(self.registry, prefix, max_results)

    def _paths(self, collection, prefix, max_results):
        assert max_results == 100
        if self.list_error:
            raise self.list_error
        paths = [path for path in collection if path.startswith(prefix)]
        if len(paths) > max_results:
            raise ValueError("too many")
        return paths


def plan(storage, groups=None, *, rfq=RFQ, package="1", customer="cust", parser="Parser1"):
    groups = groups or [("request-1", ["doc-1", "doc-2"], PROFILE)]
    sources, assignments = [], []
    for request_id, documents, profile in groups:
        evidence = [{"document_id": document,
                     "blob_path": f"{customer}/rfq-{rfq}/package-{package}/original-boms/{document}.csv",
                     "size_bytes": 10, "sha256": hashlib.sha256(document.encode()).hexdigest()}
                    for document in documents]
        sources.extend(evidence)
        storage.registry[f"candidates/requests/{request_id}.json"] = encoded({
            "version": "candidate-request.v2", "request_id": request_id,
            "customer_key": customer, "parser_key": parser, "profile_hash": profile,
            "source_evidence": evidence})
        assignments.append({"kind": "candidate_request", "request_id": request_id,
                            "document_ids": documents, "profile_hash": profile})
    body = {"version": "package-plan.v1", "request_fingerprint": "f" * 64,
            "job_id": "job-1", "customer_key": customer, "parser_key": parser,
            "rfq_uuid": rfq, "package_version": package, "sources": sources,
            "assignments": assignments}
    result = {**body, "plan_id": digest(body)}
    storage.registry[f"packages/{result['plan_id']}/package-plan.v1.json"] = encoded(result)
    return result


def ready(storage=None):
    storage = storage or Storage()
    member = enroll(storage, plan(storage), "request-1")
    return storage, Budget(storage, "request-1", clock=lambda: NOW), member


def finish(storage, reservation, state="failed", candidate=None):
    from bom_processor.author_budget import begin_execution
    path = f"author-budget/v1/groups/{reservation['group_id']}/slots/{reservation['slot']}.execution.json"
    existing = storage.registry.get(path)
    owner = (json.loads(existing)["execution_owner"] if existing else
             begin_execution(storage, reservation["group_id"], reservation["slot"],
                             reservation["request_id"], reservation["reservation_hash"]))
    return complete(storage, reservation["group_id"], reservation["slot"],
                    reservation["request_id"], reservation["reservation_hash"], state, candidate, execution_owner=owner)


def test_enroll_publishes_literal_policy_and_own_request_is_not_history():
    storage = Storage()
    storage.candidates["candidates/request-1/request.json"] = b"request"
    persisted = plan(storage)
    member = enroll(storage, persisted, "request-1")
    assert enroll(storage, persisted, "request-1") == member
    policy = json.loads(storage.registry[f"author-budget/v1/groups/{member['group_id']}/policy.json"])
    assert (policy["version"], policy["limit"], policy["stall_seconds"]) == ("author-budget.v1", 6, 1800)
    assert Budget(storage, "request-1", clock=lambda: NOW).inspect()["status"] == "available"
    assert max(storage.read_limits) <= 100000


def test_six_mixed_starts_do_not_reset_with_author_versions_or_failures():
    storage, budget, _ = ready()
    for index, version in enumerate(["v3", "v6", "v2", "v7", "v6", "v8"], 1):
        # Version is deliberately absent from budget identity and reserve API.
        phase = "author" if index % 2 else "author-refinement"
        reservation = budget.reserve(phase, **({"source_candidate": f"candidate-{index}",
                                              "failure_hash": "b" * 64} if index % 2 == 0 else {}))
        assert reservation["status"] == "reserved" and reservation["slot"] == index
        budget.mark_started(reservation, f"execution-{version}-{index}")
        finish(storage, reservation, "completed" if index % 2 else "failed",
               f"new-{index}" if index % 2 else None)
    assert budget.reserve("author")["reason"] == "budget_exhausted"
    assert budget.status()["used"] == 6


def test_known_dispatch_failure_consumes_slot_and_allows_next():
    storage, budget, _ = ready()
    reservation = budget.reserve("author")
    finish(storage, reservation)  # No started marker: known dispatch rejection.
    assert budget.reserve("author")["slot"] == 2


@pytest.mark.parametrize("started", [False, True])
@pytest.mark.parametrize("age,reason", [(0, "budget_in_flight"), (1799, "budget_in_flight"),
                                      (1800, "budget_stalled"), (1801, "budget_stalled")])
def test_claim_and_started_stall_boundary(started, age, reason):
    storage, budget, _ = ready()
    reservation = budget.reserve("author")
    if started:
        budget.mark_started(reservation, "execution")
    later = Budget(storage, "request-1", clock=lambda: NOW + timedelta(seconds=age))
    assert later.reserve("author")["reason"] == reason
    assert later.inspect()["used"] == 1


@pytest.mark.parametrize("terminal", [None, "failed", "completed"])
def test_unknown_immediate_and_permanent_even_if_terminal_later(terminal):
    storage, budget, _ = ready()
    reservation = budget.reserve("author")
    if terminal:
        from bom_processor.author_budget import begin_execution
        begin_execution(storage, reservation["group_id"], reservation["slot"],
                        reservation["request_id"], reservation["reservation_hash"])
    budget.mark_unknown(reservation)
    if terminal:
        finish(storage, reservation, terminal, "new-candidate" if terminal == "completed" else None)
    assert budget.status()["reason"] == "budget_start_unknown"
    later = Budget(storage, "request-1", clock=lambda: NOW + timedelta(days=365))
    assert later.reserve("author")["reason"] == "budget_start_unknown"


def test_repeated_source_candidate_is_no_progress_forever():
    storage, budget, _ = ready()
    reservation = budget.reserve("author-refinement", "candidate-1", "b" * 64)
    finish(storage, reservation, "completed", "candidate-1")
    assert budget.reserve("author")["reason"] == "budget_no_progress"


def test_repeated_prior_candidate_identity_is_no_progress():
    storage, budget, _ = ready()
    finish(storage, budget.reserve("author"), "completed", "candidate-1")
    reservation = budget.reserve("author-refinement", "candidate-2", "b" * 64)
    finish(storage, reservation, "completed", "candidate-1")
    assert budget.inspect()["reason"] == "budget_no_progress"


def test_concurrent_identical_intent_has_unique_owner_and_one_dispatch():
    storage, _, _ = ready()
    storage.race = threading.Barrier(2)
    def reserve(token):
        return Budget(storage, "request-1", clock=lambda: NOW,
                      token_factory=lambda: token).reserve("author")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(reserve, ["random-owner-a", "random-owner-b"]))
    assert sorted(result["status"] for result in results) == ["hold", "reserved"]
    assert len(set(storage.claim_payloads)) == 2
    assert len([result for result in results if result["status"] == "reserved"]) == 1
    assert next(result for result in results if result["status"] == "hold")["reason"] == "budget_in_flight"


def test_default_random_claim_tokens_differ_between_independent_reservations():
    storage, budget, _ = ready()
    first = budget.reserve("author")
    finish(storage, first)
    second = budget.reserve("author")
    assert len(first["owner_token"]) == 64 and first["owner_token"] != second["owner_token"]


def test_shuffled_split_same_profile_share_group_and_active_hold():
    storage = Storage()
    full = plan(storage)
    first = enroll(storage, full, "request-1")
    split = plan(storage, [("request-b", ["doc-2"], PROFILE), ("request-a", ["doc-1"], PROFILE)])
    second = enroll(storage, split, "request-a")
    third = enroll(storage, split, "request-b")
    assert first["group_id"] == second["group_id"] == third["group_id"]
    budget = Budget(storage, "request-1", clock=lambda: NOW)
    reservation = budget.reserve("author")
    assert Budget(storage, "request-a", clock=lambda: NOW).reserve("author")["reason"] == "budget_in_flight"
    finish(storage, reservation)
    assert Budget(storage, "request-b", clock=lambda: NOW).reserve("author")["slot"] == 2


def test_membership_request_order_does_not_change_group_hash():
    left, right = Storage(), Storage()
    groups = [("r1", ["doc-1"], PROFILE), ("r2", ["doc-2"], PROFILE)]
    a = enroll(left, plan(left, groups), "r1")
    b = enroll(right, plan(right, groups[::-1]), "r2")
    assert a["group_id"] == b["group_id"]


@pytest.mark.parametrize("changes", [{"rfq": "22345678-1234-4234-8234-123456789012"},
                                    {"package": "2"}, {"customer": "other"}, {"parser": "Parser2"}])
def test_different_quote_package_or_namespace_has_different_group(changes):
    left, right = Storage(), Storage()
    a = enroll(left, plan(left), "request-1")
    b = enroll(right, plan(right, **changes), "request-1")
    assert a["group_id"] != b["group_id"]


def test_different_profiles_in_same_quote_are_independent():
    storage = Storage()
    persisted = plan(storage, [("r1", ["doc-1"], PROFILE), ("r2", ["doc-2"], "b" * 64)])
    a, b = enroll(storage, persisted, "r1"), enroll(storage, persisted, "r2")
    assert a["group_id"] != b["group_id"]
    for request in ("r1", "r2"):
        assert Budget(storage, request, clock=lambda: NOW).reserve("author")["slot"] == 1


def test_group_membership_cannot_expand():
    storage, _, _ = ready()
    expanded = plan(storage, [("request-new", ["doc-1", "doc-2", "doc-3"], PROFILE)])
    with pytest.raises(BudgetError, match="budget_invalid"):
        enroll(storage, expanded, "request-new")


@pytest.mark.parametrize("kind", ["automation", "candidate", "validator"])
def test_legacy_history_rejected(kind):
    storage = Storage()
    persisted = plan(storage)
    if kind == "automation":
        storage.registry["candidate-automation/request-1/author/v6/started.json"] = b"history"
    elif kind == "validator":
        storage.registry["candidate-automation/request-1/candidate/validator/started.json"] = b"history"
    else:
        storage.candidates["candidates/request-1/candidate/candidate-manifest.json"] = b"history"
    with pytest.raises(BudgetError):
        enroll(storage, persisted, "request-1")
    assert not any(path.startswith("author-budget/") for path in storage.registry)


@pytest.mark.parametrize("mutation", ["not_persisted", "plan_hash", "child_profile", "child_evidence", "child_namespace"])
def test_untrusted_plan_or_child_binding_rejected(mutation):
    storage = Storage()
    persisted = plan(storage)
    if mutation == "not_persisted":
        del storage.registry[f"packages/{persisted['plan_id']}/package-plan.v1.json"]
    elif mutation == "plan_hash":
        persisted["job_id"] = "forged-job"
    else:
        path = "candidates/requests/request-1.json"
        child = json.loads(storage.registry[path])
        if mutation == "child_profile":
            child["profile_hash"] = "b" * 64
        elif mutation == "child_namespace":
            child["customer_key"] = "forged"
        else:
            child["source_evidence"][0]["sha256"] = "b" * 64
        storage.registry[path] = encoded(child)
    with pytest.raises(BudgetError):
        enroll(storage, persisted, "request-1")


def test_absent_membership_legacy_distinct_from_inaccessible():
    storage = Storage()
    budget = Budget(storage, "request-1", clock=lambda: NOW)
    assert budget.inspect()["status"] == "legacy"
    assert budget.reserve("author")["reason"] == "budget_invalid"
    storage.read_error = ("members", PermissionError())
    assert budget.inspect()["status"] == "hold"
    assert budget.inspect()["reason"] == "budget_invalid"


@pytest.mark.parametrize("path", ["policy.json", "1.claim.json", "1.started.json", "1.unknown.json", "1.terminal.json"])
@pytest.mark.parametrize("error", [PermissionError(), RuntimeError(), BlobConflictError()])
def test_inaccessible_storage_is_never_vacant(path, error):
    storage, budget, _ = ready()
    storage.read_error = (path, error)
    assert budget.reserve("author")["reason"] == "budget_invalid"
    assert storage.claim_payloads == []


@pytest.mark.parametrize("error", [PermissionError(), RuntimeError(), BlobNotFoundError()])
def test_reservation_write_errors_fail_closed(error):
    storage, budget, _ = ready()
    storage.write_error = error
    assert budget.reserve("author")["reason"] == "budget_invalid"


def test_enrollment_listing_access_failure_fails_closed():
    storage = Storage()
    persisted = plan(storage)
    storage.list_error = PermissionError()
    with pytest.raises(BudgetError):
        enroll(storage, persisted, "request-1")


@pytest.mark.parametrize("mutation", ["hash", "request", "group", "slot", "claim", "membership"])
def test_terminal_verifies_exact_immutable_reservation(mutation):
    storage, budget, _ = ready()
    reservation = budget.reserve("author")
    supplied = copy.deepcopy(reservation)
    if mutation == "hash":
        supplied["reservation_hash"] = "b" * 64
    elif mutation == "request":
        supplied["request_id"] = "other"
    elif mutation == "group":
        supplied["group_id"] = "b" * 64
    elif mutation == "slot":
        supplied["slot"] = 2
    elif mutation == "claim":
        path = next(path for path in storage.registry if path.endswith(".claim.json"))
        claim = json.loads(storage.registry[path])
        claim["owner_token"] = "forged"
        storage.registry[path] = encoded(claim)
    else:
        del storage.registry["author-budget/v1/members/request-1.json"]
    with pytest.raises(BudgetError):
        finish(storage, supplied, "completed", "candidate")
    assert not any(path.endswith(".terminal.json") for path in storage.registry)


def test_terminal_replay_idempotent_but_conflicting_terminal_invalid():
    storage, budget, _ = ready()
    reservation = budget.reserve("author")
    a = finish(storage, reservation, "completed", "candidate")
    assert finish(storage, reservation, "completed", "candidate") == a
    with pytest.raises(BudgetError):
        finish(storage, reservation, "failed")


def test_marks_reject_other_request_or_forged_reservation():
    _, budget, _ = ready()
    reservation = budget.reserve("author")
    forged = {**reservation, "owner_token": "forged"}
    with pytest.raises(BudgetError):
        budget.mark_started(forged, "execution")
    with pytest.raises(BudgetError):
        Budget(budget.storage, "other").mark_unknown(reservation)


@pytest.mark.parametrize("phase,source,failure", [("validator", None, None),
                                                ("author", "candidate", None),
                                                ("author-refinement", None, "a" * 64),
                                                ("author-refinement", "candidate", None)])
def test_validation_and_invalid_phase_inputs_never_consume(phase, source, failure):
    storage, budget, _ = ready()
    assert budget.reserve(phase, source, failure)["reason"] == "budget_invalid"
    assert budget.status()["used"] == 0 and storage.claim_payloads == []


@pytest.mark.parametrize("raw", [b"null", b"{}", b"not-json", b"x" * 100001],
                         ids=["null", "empty-object", "invalid-json", "oversize"])
def test_malformed_or_oversize_policy_never_allows_reservation(raw):
    storage, budget, member = ready()
    storage.registry[f"author-budget/v1/groups/{member['group_id']}/policy.json"] = raw
    assert budget.reserve("author")["reason"] == "budget_invalid"


def test_inspection_scans_only_fixed_six_slots_and_is_read_only():
    storage, budget, _ = ready()
    before = dict(storage.registry)
    assert budget.inspect()["status"] == "available"
    assert storage.registry == before
    assert budget.status() == budget.inspect()


def test_uncertain_claim_write_never_returns_dispatch_permission():
    storage, budget, _ = ready()
    original = storage.upload_registry_immutable
    def uncertain(path, data, content_type):
        original(path, data, content_type)
        if path.endswith(".claim.json"):
            raise RuntimeError("response lost after durable write")
    storage.upload_registry_immutable = uncertain
    assert budget.reserve("author")["reason"] == "budget_invalid"
    storage.upload_registry_immutable = original
    assert budget.reserve("author")["reason"] == "budget_in_flight"
    assert budget.inspect()["used"] == 1


@pytest.mark.parametrize("kind", ["started", "unknown", "terminal"])
def test_orphan_marker_never_makes_slot_vacant(kind):
    storage, budget, member = ready()
    path = f"author-budget/v1/groups/{member['group_id']}/slots/1.{kind}.json"
    storage.registry[path] = encoded({"state": kind})
    assert budget.reserve("author")["reason"] == "budget_invalid"


def test_tampered_terminal_binding_is_invalid_not_available():
    storage, budget, _ = ready()
    reservation = budget.reserve("author")
    finish(storage, reservation)
    path = next(path for path in storage.registry if path.endswith(".terminal.json"))
    marker = json.loads(storage.registry[path])
    marker["reservation_hash"] = "b" * 64
    storage.registry[path] = encoded(marker)
    assert budget.reserve("author")["reason"] == "budget_invalid"


def test_terminal_without_reservation_cannot_create_accounting_history():
    storage, _, member = ready()
    with pytest.raises(BudgetError):
        complete(storage, member["group_id"], 1, "request-1", "b" * 64, "failed")
    assert not any("/slots/" in path for path in storage.registry)


def test_same_profile_split_requests_collectively_exhaust_six_slots():
    storage = Storage()
    full = plan(storage)
    enroll(storage, full, "request-1")
    split = plan(storage, [("r1", ["doc-1"], PROFILE), ("r2", ["doc-2"], PROFILE)])
    enroll(storage, split, "r1")
    enroll(storage, split, "r2")
    for slot in range(1, 7):
        request = ["request-1", "r1", "r2"][(slot - 1) % 3]
        budget = Budget(storage, request, clock=lambda: NOW)
        reservation = budget.reserve("author" if slot % 2 else "author-refinement",
                                     None if slot % 2 else f"candidate-{slot}",
                                     None if slot % 2 else "b" * 64)
        assert reservation["slot"] == slot
        finish(storage, reservation)
    for request in ("request-1", "r1", "r2"):
        assert Budget(storage, request, clock=lambda: NOW).reserve("author")["reason"] == "budget_exhausted"