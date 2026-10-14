"""Reserve one authorized failed-refinement recovery; no dispatch or network.

Inputs: immutable trusted authorization and budget records. Output: next-slot claim
or fail-closed hold. Run: python -m pytest tests/test_author_recovery.py.
"""
from .author_budget import (Budget, BlobConflictError, LIMIT, _bytes, _hash, _identifier,
                           _membership, _now, _read, _require,
                           _reservation, _slot_path, _digest)

AUTH_VERSION = "author-recovery-authorization.v1"
CLAIM_VERSION = "author-budget-recovery.v1"


def authorization_path(group_id, failed_slot):
    _digest(group_id)
    _require(type(failed_slot) is int and 1 <= failed_slot < LIMIT)
    return f"author-budget/v1/groups/{group_id}/recoveries/{failed_slot}.authorization.json"


def reserve_recovery(budget: Budget, authorization: dict):
    """The explicit service supplies trusted authority; polling never calls this.

    The authorization is already persisted by an authenticated service. This
    function cannot grant authority and never replays dispatch after a crash.
    """
    try:
        expected = {"version", "request_id", "group_id", "plan_id", "failed_slot",
                    "failed_reservation_hash", "failed_terminal_hash", "next_slot",
                    "source_candidate", "failure_hash", "actor_id", "reason",
                    "idempotency_key", "authorization_hash", "caller_application"}
        _require(type(authorization) is dict and set(authorization) == expected)
        body = {key: value for key, value in authorization.items() if key != "authorization_hash"}
        _require(authorization["version"] == AUTH_VERSION
                 and authorization["authorization_hash"] == _hash(body)
                 and authorization["request_id"] == budget.request_id)
        for key in ("actor_id", "idempotency_key", "source_candidate", "caller_application"):
            _identifier(authorization[key])
        for key in ("plan_id", "failed_reservation_hash", "failed_terminal_hash", "failure_hash"):
            _digest(authorization[key])
        _require(isinstance(authorization["reason"], str)
                 and 0 < len(authorization["reason"].strip()) <= 500)
        group = authorization["group_id"]
        failed_slot = authorization["failed_slot"]
        path = authorization_path(group, failed_slot)
        _require(_read(budget.storage, path) == authorization)
        member, _ = _membership(budget.storage, budget.request_id)
        _require(member["group_id"] == group and member["plan_id"] == authorization["plan_id"])
        next_slot = authorization["next_slot"]
        _require(type(next_slot) is int and next_slot == failed_slot + 1)
        existing = _read(budget.storage, _slot_path(group, next_slot, "claim"))
        if existing is not None:
            _reservation(budget.storage, group, next_slot, budget.request_id, existing.get("reservation_hash"))
            _require(existing.get("recovery_authorization_hash") == authorization["authorization_hash"])
            return {"status": "hold", "reason": "budget_recovery_consumed", "group_id": group,
                    "slot": next_slot, "reservation_hash": existing["reservation_hash"]}
        snapshot = []
        state = budget._inspect(snapshot)
        _require(state["status"] == "available" and state["used"] == failed_slot)
        failed_claim, terminal = snapshot[-1]
        _require(terminal is not None and terminal["state"] == "failed"
                 and terminal.get("candidate_id") is None
                 and failed_claim["version"] == "author-budget.v1"
                 and failed_claim["phase"] == "author-refinement"
                 and failed_claim["request_id"] == budget.request_id
                 and failed_claim["reservation_hash"] == authorization["failed_reservation_hash"]
                 and _hash(terminal) == authorization["failed_terminal_hash"]
                 and failed_claim["source_candidate"] == authorization["source_candidate"]
                 and failed_claim["failure_hash"] == authorization["failure_hash"])
        # Use this same verified snapshot: no rescan can advance the slot after a race.
        selected = None
        for claim, outcome in snapshot:
            if claim["request_id"] == budget.request_id and outcome and outcome["state"] == "completed":
                selected = outcome["candidate_id"]
        _require(selected == authorization["source_candidate"])
        body = {"version": CLAIM_VERSION, "group_id": group, "slot": next_slot,
                "request_id": budget.request_id, "membership_hash": _hash(member),
                "phase": "author-refinement", "source_candidate": selected,
                "failure_hash": authorization["failure_hash"], "claimed_at": _now(budget.clock),
                "owner_token": budget.token_factory(),
                "recovery_authorization_hash": authorization["authorization_hash"],
                "failed_reservation_hash": failed_claim["reservation_hash"]}
        _identifier(body["owner_token"])
        claim = {**body, "reservation_hash": _hash(body)}
        try:
            budget.storage.upload_registry_immutable(_slot_path(group, next_slot, "claim"),
                _bytes(claim), "application/json")
        except BlobConflictError:
            return budget._hold("budget_in_flight", next_slot, group)
        _require(_read(budget.storage, _slot_path(group, next_slot, "claim")) == claim)
        return {"status": "reserved", **claim}
    except Exception:
        return budget._hold("budget_invalid")


def validate_recovery_claim(storage, claim):
    """Bind v2 claims back to existing immutable failed history, never fabricate it."""
    group, slot = claim["group_id"], claim["slot"]
    authorization = _read(storage, authorization_path(group, slot - 1))
    _require(isinstance(authorization, dict)
             and authorization.get("authorization_hash") == _hash({key: value for key, value in authorization.items() if key != "authorization_hash"})
             and authorization.get("authorization_hash") == claim.get("recovery_authorization_hash")
             and authorization.get("version") == AUTH_VERSION
             and authorization.get("request_id") == claim["request_id"]
             and authorization.get("group_id") == group
             and authorization.get("next_slot") == slot
             and authorization.get("failed_slot") == slot - 1
             and authorization.get("failed_reservation_hash") == claim.get("failed_reservation_hash")
             and authorization.get("source_candidate") == claim["source_candidate"]
             and authorization.get("failure_hash") == claim["failure_hash"])
    prior = _read(storage, _slot_path(group, slot - 1, "claim"))
    terminal = _read(storage, _slot_path(group, slot - 1, "terminal"))
    _require(isinstance(prior, dict) and prior.get("reservation_hash") == claim["failed_reservation_hash"]
             and prior.get("version") == "author-budget.v1"
             and isinstance(terminal, dict) and terminal.get("state") == "failed"
             and _hash(terminal) == authorization.get("failed_terminal_hash"))


def recovery_projection(budget, authority):
    """Read the fixed consumed slot without granting dispatch permission."""
    group, slot = authority["group_id"], authority["next_slot"]
    claim = _read(budget.storage, _slot_path(group, slot, "claim"))
    if claim is None:
        return None
    _reservation(budget.storage, group, slot, budget.request_id, claim.get("reservation_hash"))
    _require(claim.get("recovery_authorization_hash") == authority["authorization_hash"])
    state = budget.inspect()
    _require(state.get("reason") != "budget_invalid")
    started = _read(budget.storage, _slot_path(group, slot, "started"))
    terminal = _read(budget.storage, _slot_path(group, slot, "terminal"))
    unknown = _read(budget.storage, _slot_path(group, slot, "unknown"))
    dispatch = _read(budget.storage, _slot_path(group, slot, "dispatch"))
    outcome = ("start_unknown" if unknown or (dispatch and not started) else
               terminal["state"] if terminal else "started" if started else "reserved")
    return {"status": "hold", "reason": "budget_recovery_consumed",
            "request_id": budget.request_id, "slot": slot, "outcome": outcome,
            "execution": started["execution"] if started else None,
            "candidate_id": terminal.get("candidate_id") if terminal else None}


class RecoveryService:
    """Internal explicit-action service; callers must authenticate before construction.

    No HTTP routing is provided here. Actor is a separately verified service argument,
    never copied from the submitted recovery body.
    """
    def __init__(self, storage, automation, *, clock=None):
        self.storage, self.automation = storage, automation
        self.clock = clock

    def recover(self, value, *, actor_id, caller_application):
        from .package_writer_gate import package_writer
        self._validate_input(value, actor_id, caller_application)
        with package_writer(self.storage, value["plan_id"], "recovery") as guard:
            return self._recover_locked(value, actor_id=actor_id,
                                        caller_application=caller_application, guard=guard)

    @staticmethod
    def _validate_input(value, actor_id, caller_application):
        fields = {"request_id", "group_id", "plan_id", "failed_slot",
                  "failed_reservation_hash", "failed_terminal_hash", "source_candidate",
                  "failure_hash", "idempotency_key", "reason"}
        _require(type(value) is dict and set(value) == fields)
        _identifier(actor_id)
        _identifier(caller_application)
        for key in ("request_id", "source_candidate", "idempotency_key"):
            _identifier(value[key])
        for key in ("group_id", "plan_id", "failed_reservation_hash", "failed_terminal_hash", "failure_hash"):
            _digest(value[key])
        _require(isinstance(value["reason"], str) and 0 < len(value["reason"].strip()) <= 500)
        authorization_path(value["group_id"], value["failed_slot"])

    def lookup(self, value, *, actor_id, caller_application):
        self._validate_input(value, actor_id, caller_application)
        body = {"version": AUTH_VERSION, **value, "next_slot": value["failed_slot"] + 1,
                "actor_id": actor_id, "caller_application": caller_application}
        authority = {**body, "authorization_hash": _hash(body)}
        _require(_read(self.storage, authorization_path(value["group_id"], value["failed_slot"])) == authority)
        binding = _membership(self.storage, value["request_id"])
        _require(binding is not None and binding[0]["group_id"] == value["group_id"]
                 and binding[0]["plan_id"] == value["plan_id"])
        options = {"clock": self.clock} if self.clock is not None else {}
        result = recovery_projection(Budget(self.storage, value["request_id"], **options), authority)
        _require(result is not None)
        return result

    def _published_candidates(self, request_id, snapshot, require):
        root = f"candidates/{request_id}/"
        paths = self.storage.list_candidate_paths(root, 100)
        require(type(paths) is list and len(paths) < 100)
        require(all(isinstance(path, str) and path.startswith(root)
                    and "\\" not in path and not any(part in {"", ".", ".."} for part in path.split("/"))
                    for path in paths))
        require(len(set(paths)) == len(paths))
        completed = {terminal["candidate_id"] for claim, terminal in snapshot
                     if claim["request_id"] == request_id and terminal is not None
                     and terminal["state"] == "completed"}
        for path in paths:
            if path.endswith("/candidate-manifest.json"):
                candidate = path[len(root):-len("/candidate-manifest.json")]
                require(isinstance(candidate, str) and "/" not in candidate)
                from .author_budget import BudgetError
                try:
                    _identifier(candidate)
                except BudgetError:
                    require(False)
                require(candidate in completed)

    def _recover_locked(self, value, *, actor_id, caller_application, guard):
        from .package_resolution import ParentPackageService
        from .candidate_status import CandidateStatusService
        from .author_budget import _put
        require = guard.require
        slot = value["failed_slot"]
        path = authorization_path(value["group_id"], slot)
        binding = _membership(self.storage, value["request_id"])
        require(binding is not None)
        member, _ = binding
        require(member["group_id"] == value["group_id"] and member["plan_id"] == value["plan_id"])
        options = {"clock": self.clock} if self.clock is not None else {}
        budget = Budget(self.storage, value["request_id"], **options)
        existing = _read(self.storage, path)
        body = {"version": AUTH_VERSION, **value, "next_slot": slot + 1, "actor_id": actor_id,
            "caller_application": caller_application}
        authority = {**body, "authorization_hash": _hash(body)}
        if existing is not None:
            require(existing == authority)
            consumed = recovery_projection(budget, authority)
            if consumed is not None:
                return consumed
        packages = ParentPackageService(self.storage)
        plan = packages._plan(value["plan_id"])
        require(any(item.get("request_id") == value["request_id"] for item in plan["assignments"]))
        require(not self.storage.list_registry_paths(f"packages/{value['plan_id']}/approvals/", 100))
        # Status without automation proves no successor/approval; this cannot dispatch.
        status = CandidateStatusService(self.storage).get(value["request_id"])
        require(status.get("candidate_id") == value["source_candidate"]
                 and status.get("status") in {"generated_unvalidated", "schema_invalid"})
        feedback = CandidateStatusService(self.storage)._latest_validation_failure(
            value["request_id"], value["source_candidate"])
        require(isinstance(feedback, dict) and feedback.get("repairable") is True
                 and _hash(feedback) == value["failure_hash"])
        scoped = self.automation._request_policy(value["request_id"])
        require(scoped.author_starter is not None)
        snapshot = []
        state = budget._inspect(snapshot)
        require(state["status"] == "available" and state["used"] == slot)
        self._published_candidates(value["request_id"], snapshot, require)
        if existing is None:
            # Before durable authority exists, prove the exact latest failed history.
            prior, terminal = snapshot[-1]
            require(prior["request_id"] == value["request_id"] and prior["phase"] == "author-refinement"
                     and prior["version"] == "author-budget.v1"
                     and prior["reservation_hash"] == value["failed_reservation_hash"]
                     and prior["source_candidate"] == value["source_candidate"]
                     and prior["failure_hash"] == value["failure_hash"]
                     and terminal is not None and terminal["state"] == "failed"
                     and _hash(terminal) == value["failed_terminal_hash"])
            guard.write_attempted = True
            _put(self.storage, path, authority)
        else:
            require(existing == authority)
        guard.write_attempted = True
        reservation = reserve_recovery(budget, authority)
        if reservation["status"] != "reserved":
            return {"status": "hold", "reason": reservation["reason"],
                    "request_id": value["request_id"], "slot": slot + 1}
        environment = {"REQUEST_ID": value["request_id"], **scoped.author_environment,
                       "AUTHOR_BUDGET_GROUP": reservation["group_id"],
                       "AUTHOR_BUDGET_SLOT": str(reservation["slot"]),
                       "AUTHOR_BUDGET_RESERVATION": reservation["reservation_hash"],
                       "AUTHOR_FEEDBACK": (f"Previous parser failed with {feedback.get('failure') or feedback.get('error_code')}: "
                                           f"{feedback.get('failure_message') or ''}")[:1000]}
        budget.mark_dispatch(reservation)
        try:
            execution = scoped.author_starter.start(environment)
            budget.mark_started(reservation, execution)
            guard.write_completed = True
        except Exception:
            try:
                budget.mark_unknown(reservation)
            except Exception:
                pass  # Existing immutable claim prevents another dispatch.
            return {"status": "hold", "reason": "budget_start_unknown",
                    "request_id": value["request_id"], "slot": slot + 1}
        return {"status": "started", "request_id": value["request_id"],
                "slot": slot + 1, "execution": execution}