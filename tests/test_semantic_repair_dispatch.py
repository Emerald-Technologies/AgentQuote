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