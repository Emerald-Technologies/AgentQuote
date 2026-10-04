"""Offline shared-budget controller/planning tests; run pytest from repo root."""
import hashlib
import json
from datetime import datetime, timezone

import pytest

from bom_processor import InMemoryStorage, ProcessingRequest
from bom_processor.author_budget import Budget, complete, enroll
from bom_processor.candidate_automation import CandidateAutomationService
from bom_processor.candidates import AggregateCandidateRequiredError
from bom_processor.remote import RemoteBomProcessor

NOW = datetime(2026, 10, 4, tzinfo=timezone.utc)


def package(storage, count=2, reverse=False):
    sources = []
    for index in range(count):
        data = b"item,qty\nA,2\n"
        path = f"cust/rfq-12345678-1234-4234-8234-123456789012/package-1/original-boms/doc-{index}.csv"
        storage.sources[path] = data
        sources.append({"document_id": f"doc-{index}", "blob_path": path, "size_bytes": len(data),
                        "sha256": hashlib.sha256(data).hexdigest()})
    return ProcessingRequest.from_dict({"job_id": "budget-package", "customer_key": "cust", "parser_key": "Parser1",
        "rfq_uuid": "12345678-1234-4234-8234-123456789012", "package_version": "1",
        "documents": list(reversed(sources)) if reverse else sources})


def planned(count=2):
    storage = InMemoryStorage()
    processor = RemoteBomProcessor(storage, None, author_budget_enabled=True)
    with pytest.raises(AggregateCandidateRequiredError) as error:
        processor.process(package(storage, count))
    request_id = error.value.candidates[0].request_id
    return storage, processor, request_id, error.value


class Starter:
    def __init__(self):
        self.calls = []
    def start(self, values):
        self.calls.append(values)
        return f"execution-{len(self.calls)}"


def finish(storage, values, state="failed", candidate=None):
    from bom_processor.author_budget import begin_execution
    owner = begin_execution(storage, values["AUTHOR_BUDGET_GROUP"], int(values["AUTHOR_BUDGET_SLOT"]),
                            values["REQUEST_ID"], values["AUTHOR_BUDGET_RESERVATION"])
    complete(storage, values["AUTHOR_BUDGET_GROUP"], int(values["AUTHOR_BUDGET_SLOT"]), values["REQUEST_ID"],
             values["AUTHOR_BUDGET_RESERVATION"], state, candidate, execution_owner=owner)


def test_fifty_sources_are_covered_and_order_does_not_reset_group():
    storage, processor, request_id, error = planned(50)
    assert len(error.candidates) == 1 and len(error.candidates[0].document_ids) == 50
    group = Budget(storage, request_id, clock=lambda: NOW).inspect()["group_id"]
    with pytest.raises(AggregateCandidateRequiredError) as shuffled:
        processor.process(package(storage, 50, reverse=True))
    assert shuffled.value.candidates[0].request_id == request_id
    assert Budget(storage, request_id, clock=lambda: NOW).inspect()["group_id"] == group


def test_initial_and_refinement_share_six_dispatches_then_hold():
    storage, _, request_id, _ = planned()
    starter = Starter()
    automation = CandidateAutomationService(storage, starter, author_budget_enabled=True, budget_clock=lambda: NOW)
    assert automation.start_author(request_id) == "started"
    assert automation.start_author(request_id) == "budget_in_flight"
    finish(storage, starter.calls[-1], "completed", "candidate-0")
    for index in range(1, 6):
        assert automation.start_author_refinement(request_id, f"candidate-{index-1}", {"failure_message": "bad header"}) == "started"
        finish(storage, starter.calls[-1], "completed", f"candidate-{index}")
    assert automation.start_author_refinement(request_id, "candidate-5", {}) == "budget_exhausted"
    assert len(starter.calls) == 6


def test_ambiguous_start_never_dispatches_second_execution():
    storage, _, request_id, _ = planned()
    class Uncertain(Starter):
        def start(self, values):
            self.calls.append(values)
            raise TimeoutError()
    starter = Uncertain()
    automation = CandidateAutomationService(storage, starter, author_budget_enabled=True, budget_clock=lambda: NOW)
    assert automation.start_author(request_id) == "budget_start_unknown"
    assert automation.start_author(request_id) == "budget_start_unknown"
    assert len(starter.calls) == 1


def test_repeated_candidate_holds_and_cannot_consume_new_slot():
    storage, _, request_id, _ = planned()
    starter = Starter()
    automation = CandidateAutomationService(storage, starter, author_budget_enabled=True, budget_clock=lambda: NOW)
    automation.start_author(request_id)
    finish(storage, starter.calls[-1], "completed", "same")
    automation.start_author_refinement(request_id, "same", {})
    finish(storage, starter.calls[-1], "completed", "same")
    assert automation.start_author_refinement(request_id, "same", {}) == "budget_no_progress"
    assert len(starter.calls) == 2


def test_missing_membership_with_required_intent_never_dispatches():
    from bom_processor.author_budget import require_budget
    storage = InMemoryStorage()
    require_budget(storage, "pending-request")
    starter = Starter()
    automation = CandidateAutomationService(storage, starter, author_budget_enabled=True, budget_clock=lambda: NOW)
    assert automation.start_author("pending-request") == "budget_invalid"
    assert not starter.calls


def test_ownerless_terminal_cannot_release_reservation():
    storage, _, request_id, _ = planned()
    reservation = Budget(storage, request_id, clock=lambda: NOW).reserve("author")
    with pytest.raises(Exception):
        complete(storage, reservation["group_id"], reservation["slot"], request_id,
                 reservation["reservation_hash"], "completed", "invented")
    assert Budget(storage, request_id, clock=lambda: NOW).inspect()["reason"] == "budget_in_flight"


def test_orphan_dispatch_is_invalid_not_available():
    storage, _, request_id, _ = planned()
    budget = Budget(storage, request_id, clock=lambda: NOW)
    group = budget.inspect()["group_id"]
    storage.registry[f"author-budget/v1/groups/{group}/slots/1.dispatch.json"] = b'{}'
    assert budget.inspect()["reason"] == "budget_invalid"


def test_worker_reservation_can_be_used_only_once():
    from bom_processor.author_budget import begin_execution
    storage, _, request_id, _ = planned()
    reservation = Budget(storage, request_id, clock=lambda: NOW).reserve("author")
    args = (storage, reservation["group_id"], reservation["slot"], request_id, reservation["reservation_hash"])
    owner = begin_execution(*args)
    with pytest.raises(Exception):
        begin_execution(*args)
    complete(*args, "completed", "new-candidate", execution_owner=owner)
    with pytest.raises(Exception):
        begin_execution(*args)


def test_dispatch_without_acknowledgment_remains_unknown_after_worker_completes():
    from bom_processor.author_budget import begin_execution
    storage, _, request_id, _ = planned()
    budget = Budget(storage, request_id, clock=lambda: NOW)
    reservation = budget.reserve("author")
    budget.mark_dispatch(reservation)
    owner = begin_execution(storage, reservation["group_id"], reservation["slot"], request_id, reservation["reservation_hash"])
    complete(storage, reservation["group_id"], reservation["slot"], request_id,
             reservation["reservation_hash"], "completed", "candidate", execution_owner=owner)
    assert budget.inspect()["reason"] == "budget_start_unknown"


def test_delayed_refinement_intent_does_not_dispatch_after_successor():
    storage, _, request_id, _ = planned()
    starter = Starter()
    automation = CandidateAutomationService(storage, starter, author_budget_enabled=True, budget_clock=lambda: NOW)
    automation.start_author(request_id)
    finish(storage, starter.calls[-1], "completed", "candidate-A")
    event = {"failure_message": "bad header"}
    assert automation.start_author_refinement(request_id, "candidate-A", event) == "started"
    finish(storage, starter.calls[-1], "completed", "candidate-B")
    assert automation.start_author_refinement(request_id, "candidate-A", event) == "budget_no_progress"
    assert len(starter.calls) == 2


@pytest.mark.parametrize("phase", ["author", "author-refinement"])
def test_completion_between_controller_check_and_reservation_prevents_dispatch(monkeypatch, phase):
    storage, _, request_id, _ = planned()
    starter = Starter()
    automation = CandidateAutomationService(storage, starter, author_budget_enabled=True, budget_clock=lambda: NOW)
    if phase == "author-refinement":
        assert automation.start_author(request_id) == "started"
        finish(storage, starter.calls[-1], "completed", "candidate-A")
    original = Budget.reserve
    intervened = False

    def paused(self, requested_phase, source_candidate=None, failure_hash=None):
        nonlocal intervened
        if not intervened:
            intervened = True
            # Other controller completes after the caller's eligibility check.
            rival = Budget(storage, request_id, clock=lambda: NOW)
            reservation = original(rival, requested_phase, source_candidate,
                                   "c" * 64 if requested_phase == "author-refinement" else None)
            assert reservation["status"] == "reserved"
            from bom_processor.author_budget import begin_execution
            rival.mark_started(reservation, "competing-execution")
            owner = begin_execution(storage, reservation["group_id"], reservation["slot"], request_id,
                                    reservation["reservation_hash"])
            complete(storage, reservation["group_id"], reservation["slot"], request_id,
                     reservation["reservation_hash"], "completed", "candidate-B", execution_owner=owner)
        return original(self, requested_phase, source_candidate, failure_hash)

    monkeypatch.setattr(Budget, "reserve", paused)
    before = len(starter.calls)
    result = (automation.start_author(request_id) if phase == "author" else
              automation.start_author_refinement(request_id, "candidate-A", {"failure_message": "different failure"}))
    assert result == "budget_no_progress"
    assert len(starter.calls) == before