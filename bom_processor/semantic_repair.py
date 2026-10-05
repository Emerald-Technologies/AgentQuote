"""Persistent semantic repair fence; no model calls or SQL authority fabrication.

Inputs: verified SQL owner adapter, immutable authority, held package writer.
Outputs: bounded CAS fence and exact replacement-validation allowance.
Run: python -m pytest tests/test_semantic_repair.py. Live adapter remains separate.
"""
import hashlib
import json
from .core import BlobNotFoundError, ProcessingError


class SemanticRepairHeld(ProcessingError):
    def __init__(self):
        super().__init__("SEMANTIC_REPAIR_HELD")


def require(ok):
    if not ok:
        raise SemanticRepairHeld()


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def pin(value):
    return type(value) is str and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def fence_path(plan):
    require(pin(plan))
    return f"packages/{plan}/coordination/semantic-repair.v1.json"


def validate_authority(value, plan):
    fields = {"version", "plan_id", "request_id", "source_candidate", "source_validation",
              "source_sha256", "output_sha256", "finding_sha256", "budget_group", "next_slot",
              "sql_intent_hash", "sql_owner", "operator_id", "caller_application", "authority_sha256"}
    require(type(value) is dict and set(value) == fields
            and value["version"] == "semantic-repair-authority.v1" and value["plan_id"] == plan
            and all(pin(value[k]) for k in ("plan_id", "request_id", "source_candidate",
                "source_validation", "source_sha256", "output_sha256", "finding_sha256",
                "budget_group", "sql_intent_hash", "sql_owner", "authority_sha256"))
            and type(value["next_slot"]) is int and 2 <= value["next_slot"] <= 6
            and all(type(value[k]) is str and 0 < len(value[k]) <= 180
                    for k in ("operator_id", "caller_application"))
            and digest({k: v for k, v in value.items() if k != "authority_sha256"}) == value["authority_sha256"])
    return value


def read_fence(storage, plan):
    try:
        raw, etag = storage.download_registry_with_etag(fence_path(plan), 100000)
    except BlobNotFoundError:
        return None, None
    try:
        value = json.loads(raw)
        require(type(value) is dict and set(value) == {"version", "plan_id", "authority", "owner",
            "phase", "sequence", "successor", "validation_id", "proof_path", "proof_sha256"}
            and value["version"] == "semantic-repair-fence.v1" and value["plan_id"] == plan
            and pin(value["owner"]) and value["phase"] in {"observing", "validation_required", "verified", "released"}
            and type(value["sequence"]) is int and value["sequence"] >= 0 and bool(etag))
        authority = validate_authority(value["authority"], plan)
        require(value["owner"] == authority["sql_owner"])
        phase = value["phase"]
        require((phase == "observing" and value["successor"] is None and value["validation_id"] is None)
            or (phase == "validation_required" and pin(value["successor"]) and value["successor"] != authority["source_candidate"] and value["validation_id"] is None)
            or (phase in {"verified", "released"} and pin(value["successor"]) and pin(value["validation_id"])))
        root = f"packages/{plan}/semantic-repair/{authority['source_candidate']}/"
        require(type(value["proof_path"]) is str and value["proof_path"].startswith(root)
                and "/../" not in value["proof_path"] and pin(value["proof_sha256"]))
        proof = json.loads(storage.download_registry(value["proof_path"], 100000))
        require(digest(proof) == value["proof_sha256"] and type(proof) is dict
                and proof.get("authority_sha256") == authority["authority_sha256"]
                and proof.get("owner") == value["owner"] and proof.get("phase") == phase
                and proof.get("sequence") == value["sequence"]
                and proof.get("successor") == value["successor"]
                and proof.get("validation_id") == value["validation_id"])
        return value, etag
    except SemanticRepairHeld:
        raise
    except Exception:
        raise SemanticRepairHeld() from None


def assert_action(storage, plan, operation, request_id=None, candidate_id=None):
    """Call only inside the acquired package writer, never grant based on flags."""
    value, _ = read_fence(storage, plan)
    if value is None or value["phase"] == "released":
        return
    require(operation == "validation" and value["phase"] == "validation_required"
            and request_id == value["authority"]["request_id"] and candidate_id == value["successor"])


class SemanticFenceCoordinator:
    """Trusted internal adapter. SQL owner verification is mandatory for every CAS.

    verify_sql(authority, expected_state) independently reads the durable record;
    no HTTP caller may supply it. Missing adapter always blocks fence writes.
    """
    def __init__(self, storage, verify_sql, authority=None):
        self.storage, self.verify_sql, self.authority = storage, verify_sql, authority

    def authorize_writer(self, plan, operation):
        """Internal repair reconciliation access, never general approval access."""
        require(operation == "recovery" and self.authority is not None)
        authority = validate_authority(self.authority, plan)
        require(callable(self.verify_sql))
        value, _ = read_fence(self.storage, plan)
        require(value is None or value["authority"] == authority)
        states = ("held", "dispatch_armed", "observing", "validation_required", "verified")
        require(any(self.verify_sql(authority, state) is True for state in states))

    def update(self, authority, writer, phase, *, successor=None, validation_id=None, evidence=None):
        authority = validate_authority(authority, authority.get("plan_id"))
        require(callable(self.verify_sql) and writer.plan_id == authority["plan_id"]
                and pin(writer.owner))
        # Check actual writer bytes; possessing an object is not ownership proof.
        raw, etag = self.storage.download_registry_with_etag(
            f"packages/{writer.plan_id}/coordination/writer.v1.json", 4096)
        held = json.loads(raw)
        require(held.get("state") == "held" and held.get("owner") == writer.owner
                and held.get("plan_id") == writer.plan_id and etag == writer.held_etag)
        before, before_etag = read_fence(self.storage, authority["plan_id"])
        states = ["observing", "validation_required", "verified", "released"]
        require(phase in states)
        require((before is None and phase == "observing") or
                (before is not None and before["authority"] == authority
                 and before["owner"] == authority["sql_owner"]
                 and states.index(phase) == states.index(before["phase"]) + 1))
        expected_sql = {"observing": "dispatch_armed", "validation_required": "observing",
                        "verified": "validation_required", "released": "verified"}[phase]
        require(self.verify_sql(authority, expected_sql) is True)
        if phase == "observing":
            require(successor is None and validation_id is None)
        else:
            require(pin(successor) and successor != authority["source_candidate"])
            if before and before["successor"] is not None:
                require(successor == before["successor"])
            require(validation_id is None if phase == "validation_required" else pin(validation_id))
            if phase == "released":
                require(validation_id == before["validation_id"])
        require(type(evidence) is dict and bool(evidence))
        sequence = 0 if before is None else before["sequence"] + 1
        proof = {"version": "semantic-repair-transition.v1", "authority_sha256": authority["authority_sha256"],
            "owner": authority["sql_owner"], "phase": phase, "sequence": sequence,
            "successor": successor, "validation_id": validation_id, "evidence": evidence}
        root = f"packages/{authority['plan_id']}/semantic-repair/{authority['source_candidate']}/"
        path = root + f"{sequence}-{phase}-{digest(proof)}.json"
        writer.write_attempted = True
        self.storage.upload_registry_immutable(path, encoded(proof), "application/json")
        value = {"version": "semantic-repair-fence.v1", "plan_id": authority["plan_id"], "authority": authority,
            "owner": authority["sql_owner"], "phase": phase, "sequence": sequence,
            "successor": successor, "validation_id": validation_id,
            "proof_path": path, "proof_sha256": digest(proof)}
        self.storage.upload_registry_if_match(fence_path(authority["plan_id"]), encoded(value), "application/json", before_etag)
        actual, _ = read_fence(self.storage, authority["plan_id"])
        require(actual == value)
        writer.write_completed = True
        return value


def assert_published_lineage(storage, request_id, snapshot):
    """No status projection may conceal a published but unaccounted successor."""
    root = f"candidates/{request_id}/"
    paths = storage.list_candidate_paths(root, 100)
    require(type(paths) is list and len(paths) < 100 and len(set(paths)) == len(paths))
    completed = {terminal["candidate_id"] for claim, terminal in snapshot
        if claim["request_id"] == request_id and terminal is not None and terminal["state"] == "completed"}
    for path in paths:
        require(type(path) is str and path.startswith(root) and "\\" not in path
                and not any(p in {"", ".", ".."} for p in path.split("/")))
        if path.endswith("/candidate-manifest.json"):
            candidate = path[len(root):-len("/candidate-manifest.json")]
            require(pin(candidate) and candidate in completed)
            manifest = json.loads(storage.download_candidate(path, 1000000))
            require(manifest.get("candidate_id") == candidate and manifest.get("request_id") == request_id)