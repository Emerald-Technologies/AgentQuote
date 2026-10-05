"""Offline semantic fence integrity tests; no SQL or cloud/model calls."""
import copy
import pytest
from bom_processor.core import BlobNotFoundError
from bom_processor.semantic_repair import (digest, encoded, assert_action,
    assert_published_lineage, SemanticRepairHeld, fence_path, SemanticFenceCoordinator)
from bom_processor.package_writer_gate import package_writer
from bom_processor.core import BlobConflictError


class Storage:
    def __init__(self): self.records = {}; self.etags = {}
    def download_registry_with_etag(self, path, limit):
        if path not in self.records: raise BlobNotFoundError("absent")
        return self.records[path], self.etags.get(path, "etag")
    def download_registry(self, path, limit): return self.download_registry_with_etag(path, limit)[0]
    download_candidate = download_registry
    def list_candidate_paths(self, prefix, limit): return [p for p in self.records if p.startswith(prefix)]
    def upload_registry_immutable(self, path, data, content_type):
        if path in self.records and self.records[path] != data: raise BlobConflictError("conflict")
        self.records[path] = data
    def upload_registry_if_match(self, path, data, content_type, etag):
        expected = self.etags.get(path, "etag") if path in self.records else None
        if expected != etag: raise BlobConflictError("conflict")
        self.records[path] = data
        self.etags[path] = str(int(self.etags.get(path, "0")) + 1)
        return self.etags[path]


def fixture(phase="observing"):
    s = Storage(); plan = "a" * 64
    body = {"version": "semantic-repair-authority.v1", "plan_id": plan,
        "request_id": "b"*64, "source_candidate": "c"*64, "source_validation": "d"*64,
        "source_sha256": "e"*64, "output_sha256": "f"*64, "finding_sha256": "1"*64,
        "budget_group": "2"*64, "next_slot": 2, "sql_intent_hash": "3"*64,
        "sql_owner": "4"*64, "operator_id": "operator", "caller_application": "app"}
    authority = {**body, "authority_sha256": digest(body)}
    successor = None if phase == "observing" else "5"*64
    validation = "6"*64 if phase in {"verified", "released"} else None
    proof = {"authority_sha256": authority["authority_sha256"], "owner": authority["sql_owner"],
        "phase": phase, "sequence": 0, "successor": successor, "validation_id": validation}
    path = f"packages/{plan}/semantic-repair/{authority['source_candidate']}/proof.json"
    s.records[path] = encoded(proof)
    fence = {"version": "semantic-repair-fence.v1", "plan_id": plan, "authority": authority,
        "owner": authority["sql_owner"], "phase": phase, "sequence": 0,
        "successor": successor, "validation_id": validation, "proof_path": path,
        "proof_sha256": digest(proof)}
    s.records[fence_path(plan)] = encoded(fence)
    return s, plan, fence


def test_absent_legacy_unchanged(): assert_action(Storage(), "a"*64, "approval")


@pytest.mark.parametrize("operation", ["approval", "recovery", "validation"])
def test_observing_blocks_all_normal_writers(operation):
    s,p,f=fixture()
    with pytest.raises(SemanticRepairHeld): assert_action(s,p,operation)


def test_only_exact_successor_validation_allowed():
    s,p,f=fixture("validation_required")
    assert_action(s,p,"validation",f["authority"]["request_id"],f["successor"])
    for request,candidate in [(f["authority"]["request_id"],f["authority"]["source_candidate"]), ("9"*64,f["successor"])]:
        with pytest.raises(SemanticRepairHeld): assert_action(s,p,"validation",request,candidate)


def test_verified_still_blocks_until_released():
    s,p,f=fixture("verified")
    with pytest.raises(SemanticRepairHeld): assert_action(s,p,"approval")
    s,p,f=fixture("released"); assert_action(s,p,"approval")


@pytest.mark.parametrize("field", ["owner", "proof_sha256", "successor"])
def test_tampering_fails_closed(field):
    s,p,f=fixture(); f[field]="9"*64; s.records[fence_path(p)]=encoded(f)
    with pytest.raises(SemanticRepairHeld): assert_action(s,p,"approval")


def test_unaccounted_published_successor_blocked():
    s=Storage(); request="b"*64; candidate="c"*64
    s.records[f"candidates/{request}/{candidate}/candidate-manifest.json"]=encoded({"request_id":request,"candidate_id":candidate})
    with pytest.raises(SemanticRepairHeld): assert_published_lineage(s,request,[])
    assert_published_lineage(s,request,[({"request_id":request},{"state":"completed","candidate_id":candidate})])


def test_real_writer_fence_lifecycle_preserves_exact_successor():
    s,p,f=fixture(); authority=f["authority"]
    del s.records[fence_path(p)]
    state=["dispatch_armed"]
    coordinator=SemanticFenceCoordinator(s,lambda a,expected: a==authority and state[0]==expected,authority)
    with package_writer(s,p,"recovery",semantic_coordinator=coordinator) as writer:
        coordinator.update(authority,writer,"observing",evidence={"sql_armed":True})
    with pytest.raises(SemanticRepairHeld):
        with package_writer(s,p,"approval"): pytest.fail("approval admitted")
    state[0]="observing"
    with package_writer(s,p,"recovery",semantic_coordinator=coordinator) as writer:
        coordinator.update(authority,writer,"validation_required",successor="5"*64,evidence={"completed_slot":2})
    with package_writer(s,p,"validation",request_id=authority["request_id"],candidate_id="5"*64): pass
    with pytest.raises(SemanticRepairHeld):
        with package_writer(s,p,"validation",request_id=authority["request_id"],candidate_id=authority["source_candidate"]): pass
    state[0]="validation_required"
    with package_writer(s,p,"recovery",semantic_coordinator=coordinator) as writer:
        coordinator.update(authority,writer,"verified",successor="5"*64,validation_id="6"*64,evidence={"independent_valid":True})
    state[0]="verified"
    with package_writer(s,p,"recovery",semantic_coordinator=coordinator) as writer:
        coordinator.update(authority,writer,"released",successor="5"*64,validation_id="6"*64,evidence={"sql_verified":True})
    with package_writer(s,p,"approval"): pass


def test_wrong_sql_owner_cannot_acquire_reconciliation_writer():
    s,p,f=fixture(); coordinator=SemanticFenceCoordinator(s,lambda a,state:False,f["authority"])
    with pytest.raises(SemanticRepairHeld):
        with package_writer(s,p,"recovery",semantic_coordinator=coordinator): pass