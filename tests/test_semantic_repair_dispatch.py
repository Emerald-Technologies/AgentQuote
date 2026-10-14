"""Offline actual-budget producer bridge checks; no SQL/cloud/model calls."""
import pytest
from test_semantic_repair import automation_fixture
from test_author_budget_integration import NOW, finish
from bom_processor.semantic_repair import encoded, digest, fence_path, SemanticRepairHeld, read_fence
from bom_processor.semantic_repair_dispatch import SemanticRepairDispatch
from bom_processor.package_writer_gate import PackageWriterBusy, package_writer


def setup():
    storage, request, plan, fence, starter, automation = automation_fixture("released")
    authority = fence["authority"]
    automation.start_author(request)
    finish(storage, starter.calls[-1], "completed", authority["source_candidate"])
    storage.candidates[f"candidates/{request}/{authority['source_candidate']}/candidate-manifest.json"] = encoded(
        {"request_id": request, "candidate_id": authority["source_candidate"]})
    del storage.registry[fence_path(plan)]
    starter.calls.clear()
    starter.start = lambda values: starter.calls.append(values) or "aq-candidate-author-test"
    state = ["dispatch_armed"]
    bridge = SemanticRepairDispatch(storage, lambda a, expected: a == authority and state[0] == expected,
        authority, lambda a: {"source": a["source_sha256"], "output": a["output_sha256"],
                             "feedback_sha256": digest("feedback")}, starter, clock=lambda: NOW)
    return storage, request, plan, authority, starter, state, bridge


def test_real_reservation_one_dispatch_releases_short_writer_only():
    storage, request, plan, authority, starter, state, bridge = setup()
    bridge.arm()
    receipt = bridge.dispatch("feedback")
    assert receipt["slot"] == 2 and len(starter.calls) == 1
    assert starter.calls[0]["AUTHOR_BUDGET_SLOT"] == "2"
    assert read_fence(storage, plan)[0]["phase"] == "observing"
    with pytest.raises(SemanticRepairHeld):
        with package_writer(storage, plan, "approval"): pass
    with pytest.raises(SemanticRepairHeld): bridge.dispatch("feedback")
    assert len(starter.calls) == 1


def test_unknown_start_retains_short_writer_fence_budget_and_never_restarts():
    storage, request, plan, authority, starter, state, bridge = setup()
    bridge.arm()
    def unknown(values):
        starter.calls.append(values)
        raise TimeoutError()
    starter.start = unknown
    with pytest.raises(TimeoutError): bridge.dispatch("feedback")
    with pytest.raises(PackageWriterBusy): bridge.dispatch("feedback")
    assert len(starter.calls) == 1 and read_fence(storage, plan)[0]["phase"] == "observing"


def test_sql_owner_or_source_revocation_stops_before_reservation():
    storage, request, plan, authority, starter, state, bridge = setup()
    bridge.arm()
    state[0] = "observing"
    with pytest.raises(SemanticRepairHeld): bridge.dispatch("feedback")
    assert not starter.calls
    assert not any("slots/2." in path for path in storage.registry)


def test_lost_receipt_holds_after_one_acknowledged_start():
    storage, request, plan, authority, starter, state, bridge = setup()
    bridge.arm()
    original = storage.upload_registry_immutable
    def write(path, data, content_type):
        if path.endswith("/started.v1.json"): raise TimeoutError()
        return original(path, data, content_type)
    storage.upload_registry_immutable = write
    with pytest.raises(TimeoutError): bridge.dispatch("feedback")
    with pytest.raises(PackageWriterBusy): bridge.dispatch("feedback")
    assert len(starter.calls) == 1


def test_changed_feedback_cannot_borrow_authority_or_reserve():
    storage, request, plan, authority, starter, state, bridge = setup()
    bridge.arm()
    with pytest.raises(SemanticRepairHeld): bridge.dispatch("different instructions")
    assert not starter.calls and not any("slots/2." in path for path in storage.registry)


def test_concurrent_bridge_call_cannot_cross_dispatch_writer():
    storage, request, plan, authority, starter, state, bridge = setup()
    bridge.arm()
    def start(values):
        starter.calls.append(values)
        with pytest.raises(PackageWriterBusy): bridge.dispatch("feedback")
        return "aq-candidate-author-test"
    starter.start = start
    bridge.dispatch("feedback")
    assert len(starter.calls) == 1


def completed_bridge():
    storage, request, plan, authority, starter, state, bridge = setup()
    bridge.arm(); bridge.dispatch("feedback")
    successor = "5"*64
    finish(storage, starter.calls[-1], "completed", successor)
    storage.candidates[f"candidates/{request}/{successor}/candidate-manifest.json"] = encoded(
        {"request_id": request, "candidate_id": successor})
    state[0] = "observing"
    return storage, request, plan, authority, starter, state, bridge, successor


def successor_proof(starter):
    return lambda a,c,e: {"candidate_id":c,"execution":e,
                         "reservation_hash":starter.calls[-1]["AUTHOR_BUDGET_RESERVATION"]}


def test_real_budget_successor_validation_and_sql_last_release():
    storage, request, plan, authority, starter, state, bridge, successor = completed_bridge()
    proof = successor_proof(starter)
    fence = bridge.authorize_successor(proof)
    assert fence["successor"] == successor and fence["phase"] == "validation_required"
    state[0] = "validation_required"
    replacement = lambda a, candidate: {"candidate_id": candidate, "validation_id": "6"*64, "output_sha256": "7"*64}
    bridge.mark_verified(replacement)
    with pytest.raises(SemanticRepairHeld): bridge.release(replacement)
    assert read_fence(storage, plan)[0]["phase"] == "verified"


def test_producer_release_replay_allows_sql_last_recovery_without_dispatch():
    storage, request, plan, authority, starter, state, bridge, successor = completed_bridge()
    bridge.authorize_successor(successor_proof(starter))
    state[0] = "validation_required"
    replacement = lambda a,c: {"candidate_id":c,"validation_id":"6"*64,"output_sha256":"7"*64}
    bridge.mark_verified(replacement)
    state[0] = "verified"
    released = bridge.release(replacement)
    assert released["phase"] == "released"
    original_immutable = storage.upload_registry_immutable
    original_cas = storage.upload_registry_if_match
    def forbidden(*args): pytest.fail("release replay attempted a write")
    storage.upload_registry_immutable = forbidden
    storage.upload_registry_if_match = forbidden
    assert bridge.release(replacement) == released
    with pytest.raises(SemanticRepairHeld):
        bridge.release(lambda a,c: {"candidate_id":c,"validation_id":"6"*64,"output_sha256":"8"*64})
    storage.upload_registry_immutable = original_immutable
    storage.upload_registry_if_match = original_cas
    assert len(starter.calls) == 1
    with package_writer(storage, plan, "approval"): pass


def test_changed_replacement_proof_cannot_release_fence():
    storage, request, plan, authority, starter, state, bridge, successor = completed_bridge()
    bridge.authorize_successor(successor_proof(starter))
    state[0] = "validation_required"
    bridge.mark_verified(lambda a,c: {"validation_id":"6"*64,"output_sha256":"7"*64})
    state[0] = "verified"
    with pytest.raises(SemanticRepairHeld):
        bridge.release(lambda a,c: {"validation_id":"6"*64,"output_sha256":"8"*64})
    assert read_fence(storage, plan)[0]["phase"] == "verified"


def test_producer_ahead_of_sql_reconstructs_without_writes_or_dispatch():
    from bom_processor.semantic_repair_dispatch import SemanticRepairDispatch
    storage, request, plan, authority, starter, state, bridge, successor = completed_bridge()
    fence = bridge.authorize_successor(successor_proof(starter))
    fresh = SemanticRepairDispatch(storage, bridge.coordinator.verify_sql, authority,
                                   bridge.verify_original, starter, clock=lambda: NOW)
    original_immutable = storage.upload_registry_immutable
    original_cas = storage.upload_registry_if_match
    def forbidden(*args): pytest.fail("producer-ahead replay attempted write")
    storage.upload_registry_immutable = forbidden; storage.upload_registry_if_match = forbidden
    assert fresh.authorize_successor(successor_proof(starter)) == fence
    storage.upload_registry_immutable = original_immutable; storage.upload_registry_if_match = original_cas
    state[0] = "validation_required"
    replacement = lambda a,c: {"candidate_id":c,"validation_id":"6"*64,"output_sha256":"7"*64}
    verified = fresh.mark_verified(replacement)
    storage.upload_registry_immutable = forbidden; storage.upload_registry_if_match = forbidden
    assert fresh.mark_verified(replacement) == verified
    assert len(starter.calls) == 1


def test_losing_stale_reconciler_releases_own_unmodified_writer():
    from contextlib import contextmanager
    from bom_processor.author_budget import BudgetError
    storage, request, plan, authority, starter, state, bridge, successor = completed_bridge()
    actual_writer = bridge._writer
    @contextmanager
    def competing_writer():
        # Caller A completes between caller B's pre-read and writer acquisition.
        bridge._writer = actual_writer
        bridge.authorize_successor(successor_proof(starter))
        state[0] = "validation_required"
        with actual_writer() as writer:
            yield writer
    bridge._writer = competing_writer
    with pytest.raises(BudgetError): bridge.authorize_successor(successor_proof(starter))
    bridge._writer = actual_writer
    with actual_writer(): pass  # Losing pre-write rejection did not strand gate.
    replacement = lambda a,c: {"candidate_id":c,"validation_id":"6"*64,"output_sha256":"7"*64}
    @contextmanager
    def competing_verification():
        bridge._writer = actual_writer
        bridge.mark_verified(replacement)
        state[0] = "verified"
        with actual_writer() as writer:
            yield writer
    bridge._writer = competing_verification
    with pytest.raises(BudgetError): bridge.mark_verified(replacement)
    bridge._writer = actual_writer
    with actual_writer(): pass
    assert read_fence(storage, plan)[0]["phase"] == "verified"
    assert len(starter.calls) == 1