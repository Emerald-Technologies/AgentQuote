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


def test_real_writer_fence_lifecycle_preserves_exact_successor(monkeypatch):
    s,p,f=fixture(); authority=f["authority"]
    # This isolates fence CAS. Real budget/worker/publication coverage is below.
    snapshot = [({"request_id": authority["request_id"], "phase": "author-refinement",
                  "source_candidate": authority["source_candidate"]},
                 {"state": "completed", "candidate_id": authority["source_candidate"]})]
    projection = {"group_id": authority["budget_group"], "used": 1}
    monkeypatch.setattr("bom_processor.semantic_repair.verified_published_budget",
                        lambda *args, **kwargs: (projection, snapshot))
    del s.records[fence_path(p)]
    state=["dispatch_armed"]
    coordinator=SemanticFenceCoordinator(s,lambda a,expected: a==authority and state[0]==expected,authority)
    with package_writer(s,p,"recovery",semantic_coordinator=coordinator) as writer:
        coordinator.update(authority,writer,"observing",evidence={"sql_armed":True})
    with pytest.raises(SemanticRepairHeld):
        with package_writer(s,p,"approval"): pytest.fail("approval admitted")
    state[0]="observing"
    projection["used"] = 2
    snapshot[-1][1]["candidate_id"] = "5"*64
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


def automation_fixture(phase):
    from test_author_budget_integration import planned, Starter, NOW
    from bom_processor.author_budget import _membership
    from bom_processor.candidate_automation import CandidateAutomationService
    storage, _, request, _ = planned()
    member, _ = _membership(storage, request)
    _, _, fence = fixture(phase)
    plan = member["plan_id"]
    authority = fence["authority"]
    authority.update(plan_id=plan, request_id=request, budget_group=member["group_id"])
    authority["authority_sha256"] = digest({k: v for k, v in authority.items() if k != "authority_sha256"})
    proof = {"authority_sha256": authority["authority_sha256"], "owner": authority["sql_owner"],
        "phase": phase, "sequence": 0, "successor": fence["successor"], "validation_id": fence["validation_id"]}
    path = f"packages/{plan}/semantic-repair/{authority['source_candidate']}/proof.json"
    fence.update(plan_id=plan, proof_path=path, proof_sha256=digest(proof))
    storage.registry[path] = encoded(proof)
    storage.registry[fence_path(plan)] = encoded(fence)
    starter = Starter()
    automation = CandidateAutomationService(storage, starter, starter, budget_clock=lambda: NOW)
    return storage, request, plan, fence, starter, automation


@pytest.mark.parametrize("phase", ["observing", "validation_required", "verified"])
@pytest.mark.parametrize("action", ["author", "refinement", "validator"])
def test_ordinary_dispatch_blocked_before_claim_or_budget_reservation(phase, action):
    storage, request, plan, fence, starter, automation = automation_fixture(phase)
    before = copy.deepcopy(storage.registry)
    with pytest.raises(SemanticRepairHeld):
        if action == "author": automation.start_author(request)
        elif action == "refinement":
            automation.start_author_refinement(request, fence["authority"]["source_candidate"], {"failure": "x"})
        else: automation.start_validator(request, fence["authority"]["source_candidate"])
    assert starter.calls == []
    writer_path = f"packages/{plan}/coordination/writer.v1.json"
    assert {k: v for k, v in storage.registry.items() if k != writer_path} == before
    assert __import__("json").loads(storage.registry[writer_path])["state"] == "idle"


def test_dispatch_allows_only_named_successor_and_holds_writer_through_start():
    from bom_processor.package_writer_gate import PackageWriterBusy
    storage, request, plan, fence, starter, automation = automation_fixture("validation_required")
    original_start = starter.start
    def start(values):
        # The coordinator cannot install a competing phase between eligibility
        # and the actual external start, even with the correct SQL authority.
        coordinator = SemanticFenceCoordinator(storage, lambda a, state: True, fence["authority"])
        with pytest.raises(PackageWriterBusy):
            with package_writer(storage, plan, "recovery", semantic_coordinator=coordinator):
                pytest.fail("concurrent coordinator acquired writer")
        return original_start(values)
    starter.start = start
    assert automation.start_validator(request, fence["successor"]) == "started"
    assert len(starter.calls) == 1
    assert automation.start_validator(request, fence["successor"]) is None
    assert len(starter.calls) == 1


def test_fast_validation_entrypoint_waits_for_own_dispatcher_without_restart():
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from bom_processor.candidate_validation import CandidateValidationService
    storage, request, plan, fence, starter, automation = automation_fixture("validation_required")
    waiting, release = threading.Event(), threading.Event()
    entered = []
    def wait(seconds):
        waiting.set()
        assert release.wait(5)
    worker = CandidateValidationService(request, fence["successor"], storage, None, sleep=wait)
    worker._validate_locked = lambda: entered.append("validated") or "result"
    with ThreadPoolExecutor(max_workers=1) as pool:
        futures = []
        def start(values):
            starter.calls.append(values)
            futures.append(pool.submit(worker.validate))
            assert waiting.wait(5)
            assert not entered
            return "execution-1"
        starter.start = start
        try:
            assert automation.start_validator(request, fence["successor"]) == "started"
        finally:
            release.set()
        assert futures[0].result(timeout=5) == "result"
    assert entered == ["validated"] and len(starter.calls) == 1


def test_guarded_author_unknown_start_keeps_budget_and_never_redispatches():
    storage, request, plan, fence, starter, automation = automation_fixture("released")
    def uncertain(values):
        starter.calls.append(values)
        raise TimeoutError()
    starter.start = uncertain
    assert automation.start_author(request) == "budget_start_unknown"
    assert automation.start_author(request) == "budget_start_unknown"
    assert len(starter.calls) == 1


def test_validator_receipt_write_loss_retains_writer_and_never_redispatches():
    from bom_processor.package_writer_gate import PackageWriterBusy
    storage, request, plan, fence, starter, automation = automation_fixture("validation_required")
    original_write = storage.upload_registry_immutable
    def write(path, data, content_type):
        if "/validator/" in path and path.endswith(".started.json"):
            raise TimeoutError()
        return original_write(path, data, content_type)
    storage.upload_registry_immutable = write
    with pytest.raises(TimeoutError): automation.start_validator(request, fence["successor"])
    with pytest.raises(PackageWriterBusy): automation.start_validator(request, fence["successor"])
    assert len(starter.calls) == 1


def test_real_budget_publication_requires_completed_worker_terminal():
    from test_author_budget_integration import planned, NOW
    from bom_processor.author_budget import Budget, begin_execution, complete
    from bom_processor.semantic_repair import verified_published_budget
    storage, _, request, _ = planned()
    budget = Budget(storage, request, clock=lambda: NOW)
    candidate = "c" * 64
    reservation = budget.reserve("author")
    budget.mark_dispatch(reservation)
    budget.mark_started(reservation, "execution-1")
    owner = begin_execution(storage, reservation["group_id"], reservation["slot"],
                            request, reservation["reservation_hash"])
    storage.candidates[f"candidates/{request}/{candidate}/candidate-manifest.json"] = encoded(
        {"request_id": request, "candidate_id": candidate})
    before = copy.deepcopy(storage.registry)
    with pytest.raises(SemanticRepairHeld):
        verified_published_budget(storage, request, clock=lambda: NOW)
    assert storage.registry == before
    complete(storage, reservation["group_id"], reservation["slot"], request,
             reservation["reservation_hash"], "completed", candidate, execution_owner=owner)
    projection, snapshot = verified_published_budget(storage, request, clock=lambda: NOW)
    assert projection["used"] == 1 and snapshot[0][1]["candidate_id"] == candidate
    storage.candidates[f"candidates/{request}/{'d'*64}/candidate-manifest.json"] = encoded(
        {"request_id": request, "candidate_id": "d"*64})
    with pytest.raises(SemanticRepairHeld):
        verified_published_budget(storage, request, clock=lambda: NOW)


def test_fence_transitions_consume_actual_same_group_worker_lineage():
    from test_author_budget_integration import NOW, finish
    from bom_processor.author_budget import Budget
    storage, request, plan, fence, starter, automation = automation_fixture("released")
    authority = fence["authority"]
    assert automation.start_author(request) == "started"
    finish(storage, starter.calls[-1], "completed", authority["source_candidate"])
    storage.candidates[f"candidates/{request}/{authority['source_candidate']}/candidate-manifest.json"] = encoded(
        {"request_id": request, "candidate_id": authority["source_candidate"]})
    del storage.registry[fence_path(plan)]
    state = ["dispatch_armed"]
    coordinator = SemanticFenceCoordinator(storage, lambda a, expected: a == authority and state[0] == expected,
                                          authority, budget_clock=lambda: NOW)
    with package_writer(storage, plan, "recovery", semantic_coordinator=coordinator) as writer:
        coordinator.update(authority, writer, "observing", evidence={"sql_armed": True})
    budget = Budget(storage, request, clock=lambda: NOW)
    reservation = budget.reserve("author-refinement", authority["source_candidate"], authority["authority_sha256"])
    assert reservation["slot"] == 2
    budget.mark_dispatch(reservation)
    budget.mark_started(reservation, "execution-2")
    values = {"REQUEST_ID": request, "AUTHOR_BUDGET_GROUP": reservation["group_id"],
              "AUTHOR_BUDGET_SLOT": "2", "AUTHOR_BUDGET_RESERVATION": reservation["reservation_hash"]}
    successor = "5" * 64
    finish(storage, values, "completed", successor)
    storage.candidates[f"candidates/{request}/{successor}/candidate-manifest.json"] = encoded(
        {"request_id": request, "candidate_id": successor})
    state[0] = "observing"
    with package_writer(storage, plan, "recovery", semantic_coordinator=coordinator) as writer:
        result = coordinator.update(authority, writer, "validation_required", successor=successor,
                                    evidence={"completed_slot": 2})
    assert result["successor"] == successor