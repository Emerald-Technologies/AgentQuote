"""Trusted semantic repair producer bridge; no HTTP or implicit activation.

Inputs: verified SQL owner, immutable authority, source verifier, storage/starter.
Output: persistent fence and one fixed-slot receipt; uncertain outcomes hold.
Run: python -m pytest tests/test_semantic_repair_dispatch.py. No retry path.
"""
import secrets
from .author_budget import Budget
from .package_writer_gate import package_writer
from .semantic_repair import (SemanticFenceCoordinator, validate_authority,
    verified_published_budget, read_fence, require, encoded, digest)


class SemanticRepairDispatch:
    """Internal only. verify_original must independently verify actual source,
    original candidate/validation/output/finding bytes, not caller booleans.
    This bridge alone is not live readiness; adapter construction owns trust.
    """
    def __init__(self, storage, verify_sql, authority, verify_original, starter, *, clock=None):
        self.storage, self.starter, self.verify_original = storage, starter, verify_original
        self.authority = validate_authority(authority, authority.get("plan_id"))
        self.clock = clock
        self.coordinator = SemanticFenceCoordinator(storage, verify_sql, self.authority, budget_clock=clock)

    def _writer(self):
        return package_writer(self.storage, self.authority["plan_id"], "recovery",
                              semantic_coordinator=self.coordinator)

    def _original(self):
        require(callable(self.verify_original))
        proof = self.verify_original(self.authority)
        require(type(proof) is dict and bool(proof))
        return proof

    def arm(self):
        with self._writer() as writer:
            proof = self._original()
            return self.coordinator.update(self.authority, writer, "observing", evidence=proof)

    def dispatch(self, feedback):
        """ONE call, never replay. Random immutable claim precedes reservation.

        SQL remains dispatch_armed until the outer coordinator persists receipt.
        A successful start releases only the short writer, not the repair fence.
        """
        require(type(feedback) is str and 0 < len(feedback) <= 1000)
        authority = self.authority
        root = f"packages/{authority['plan_id']}/semantic-repair/{authority['source_candidate']}/"
        with self._writer() as writer:
            require(self.coordinator.verify_sql(authority, "dispatch_armed") is True)
            fence, _ = read_fence(self.storage, authority["plan_id"])
            require(fence is not None and fence["authority"] == authority and fence["phase"] == "observing")
            proof = self._original()
            require(proof.get("feedback_sha256") == digest(feedback))
            projection, snapshot = verified_published_budget(self.storage, authority["request_id"], clock=self.clock)
            require(projection["status"] == "available" and projection["group_id"] == authority["budget_group"]
                    and projection["used"] == authority["next_slot"] - 1
                    and snapshot[-1][1]["candidate_id"] == authority["source_candidate"])
            claim = {"version": "semantic-repair-dispatch.v1", "authority_sha256": authority["authority_sha256"],
                     "sql_owner": authority["sql_owner"], "owner": secrets.token_hex(32),
                     "feedback_sha256": digest(feedback), "original_proof_sha256": digest(proof)}
            writer.write_attempted = True
            self.storage.upload_registry_immutable(root + "dispatch-claim.v1.json", encoded(claim), "application/json")
            require(self.storage.download_registry(root + "dispatch-claim.v1.json", 100000) == encoded(claim))
            require(self.coordinator.verify_sql(authority, "dispatch_armed") is True)
            require(self._original() == proof)
            budget = Budget(self.storage, authority["request_id"], **({"clock": self.clock} if self.clock else {}))
            reservation = budget.reserve("author-refinement", authority["source_candidate"], authority["authority_sha256"])
            require(reservation.get("status") == "reserved" and reservation.get("group_id") == authority["budget_group"]
                    and reservation.get("slot") == authority["next_slot"]
                    and reservation.get("source_candidate") == authority["source_candidate"]
                    and reservation.get("failure_hash") == authority["authority_sha256"])
            self.storage.upload_registry_immutable(root + "reservation.v1.json", encoded(reservation), "application/json")
            budget.mark_dispatch(reservation)
            values = {"REQUEST_ID": authority["request_id"], "BOM_SOURCE_ASSOCIATIONS": "true", "AUTHOR_FEEDBACK": feedback,
                "AUTHOR_BUDGET_GROUP": authority["budget_group"], "AUTHOR_BUDGET_SLOT": str(authority["next_slot"]),
                "AUTHOR_BUDGET_RESERVATION": reservation["reservation_hash"]}
            execution = self.starter.start(values)
            require(type(execution) is str and execution.startswith("aq-candidate-author-"))
            budget.mark_started(reservation, execution)
            receipt = {"authority_sha256": authority["authority_sha256"], "sql_owner": authority["sql_owner"],
                       "reservation_hash": reservation["reservation_hash"], "slot": authority["next_slot"],
                       "execution": execution}
            self.storage.upload_registry_immutable(root + "started.v1.json", encoded(receipt), "application/json")
            require(self.storage.download_registry(root + "started.v1.json", 100000) == encoded(receipt))
            writer.write_completed = True
            return receipt

    def authorize_successor(self, verify_successor):
        """Explicit reconciliation only; a persisted transition replays read-only."""
        authority = self.authority
        fence, _ = read_fence(self.storage, authority["plan_id"])
        if fence is not None and fence["phase"] == "validation_required":
            require(fence["authority"] == authority
                    and self.coordinator.verify_sql(authority, "observing") is True)
            proof = self._successor_proof(verify_successor)
            self._require_transition(fence, proof)
            require(fence["successor"] == proof["candidate_id"])
            return fence
        with self._writer() as writer:
            writer.require(self.coordinator.verify_sql(authority, "observing") is True)
            proof = self._successor_proof(verify_successor)
            fence, _ = read_fence(self.storage, authority["plan_id"])
            require(fence is not None and fence["authority"] == authority)
            if fence["phase"] == "validation_required":
                self._require_transition(fence, proof)
                require(fence["successor"] == proof["candidate_id"])
                return fence
            return self.coordinator.update(
                authority, writer, "validation_required",
                successor=proof["candidate_id"], evidence=proof)

    def _successor_proof(self, verify_successor):
        authority = self.authority
        self._original()
        projection, snapshot = verified_published_budget(
            self.storage, authority["request_id"], clock=self.clock)
        require(projection["group_id"] == authority["budget_group"]
                and projection["used"] == authority["next_slot"])
        claim, terminal = snapshot[-1]
        require(claim["request_id"] == authority["request_id"]
                and claim["phase"] == "author-refinement"
                and claim["source_candidate"] == authority["source_candidate"]
                and claim["failure_hash"] == authority["authority_sha256"]
                and terminal is not None and terminal["state"] == "completed")
        root = f"packages/{authority['plan_id']}/semantic-repair/{authority['source_candidate']}/"
        import json
        receipt = json.loads(self.storage.download_registry(root + "started.v1.json", 100000))
        started = json.loads(self.storage.download_registry(
            f"author-budget/v1/groups/{authority['budget_group']}/slots/{authority['next_slot']}.started.json", 100000))
        require(receipt.get("authority_sha256") == authority["authority_sha256"]
                and receipt.get("sql_owner") == authority["sql_owner"]
                and receipt.get("reservation_hash") == claim["reservation_hash"]
                and receipt.get("execution") == started.get("execution")
                and receipt.get("slot") == authority["next_slot"]
                and callable(verify_successor))
        proof = verify_successor(authority, terminal["candidate_id"], receipt["execution"])
        require(type(proof) is dict
                and proof.get("candidate_id") == terminal["candidate_id"]
                and proof.get("reservation_hash") == claim["reservation_hash"])
        return {**proof, "terminal_sha256": digest(terminal), "receipt_sha256": digest(receipt)}

    def _require_transition(self, fence, proof):
        import json
        transition = json.loads(self.storage.download_registry(fence["proof_path"], 100000))
        require(transition["evidence"] == proof)

    def mark_verified(self, verify_replacement):
        """Verify exact validation/merge; producer-ahead SQL recovery is read-only."""
        authority = self.authority
        fence, _ = read_fence(self.storage, authority["plan_id"])
        if fence is not None and fence["phase"] == "verified":
            require(fence["authority"] == authority
                    and self.coordinator.verify_sql(authority, "validation_required") is True)
            self._original()
            proof = verify_replacement(authority, fence["successor"])
            self._require_transition(fence, proof)
            require(type(proof) is dict and proof.get("validation_id") == fence["validation_id"])
            return fence
        with self._writer() as writer:
            writer.require(self.coordinator.verify_sql(authority, "validation_required") is True)
            fence, _ = read_fence(self.storage, authority["plan_id"])
            require(fence is not None and fence["authority"] == authority
                    and fence["phase"] in {"validation_required", "verified"})
            self._original()
            require(callable(verify_replacement))
            proof = verify_replacement(authority, fence["successor"])
            require(type(proof) is dict and bool(proof))
            if fence["phase"] == "verified":
                self._require_transition(fence, proof)
                require(proof.get("validation_id") == fence["validation_id"])
                return fence
            return self.coordinator.update(
                authority, writer, "verified", successor=fence["successor"],
                validation_id=proof.get("validation_id"), evidence=proof)

    def release(self, verify_replacement):
        """Release producer after SQL verified; an already-released replay only reads."""
        authority = self.authority
        fence, _ = read_fence(self.storage, authority["plan_id"])
        if fence is not None and fence["phase"] == "released":
            return self._released_proof(fence, verify_replacement)
        with self._writer() as writer:
            require(self.coordinator.verify_sql(authority, "verified") is True)
            fence, _ = read_fence(self.storage, authority["plan_id"])
            require(fence is not None and fence["authority"] == authority
                    and fence["phase"] in {"verified", "released"})
            if fence["phase"] == "released":
                return self._released_proof(fence, verify_replacement)
            self._original()
            proof = verify_replacement(authority, fence["successor"])
            require(type(proof) is dict and proof.get("validation_id") == fence["validation_id"])
            self._require_transition(fence, proof)
            return self.coordinator.update(
                authority, writer, "released", successor=fence["successor"],
                validation_id=fence["validation_id"],
                evidence={"replacement_proof_sha256": digest(proof),
                          "sql_verified": authority["sql_intent_hash"]})

    def _released_proof(self, fence, verify_replacement):
        require(fence["authority"] == self.authority
                and self.coordinator.verify_sql(self.authority, "verified") is True)
        self._original()
        proof = verify_replacement(self.authority, fence["successor"])
        require(type(proof) is dict and proof.get("validation_id") == fence["validation_id"])
        self._require_transition(
            fence, {"replacement_proof_sha256": digest(proof),
                    "sql_verified": self.authority["sql_intent_hash"]})
        return fence