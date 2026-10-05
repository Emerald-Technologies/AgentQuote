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