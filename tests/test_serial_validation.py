"""Offline asynchronous package-slot tests; no live job starts."""
import json

import pytest

from bom_processor.candidate_automation import CandidateAutomationService
from bom_processor.serial_validation import reserve, transition, SerialValidationHeld
from test_author_budget_integration import planned, Starter


def test_one_in_flight_and_unknown_start_never_retries():
    storage, _, request, _ = planned()
    starter = Starter()
    service = CandidateAutomationService(storage, validator_starter=starter)
    assert service.start_validator(request, "candidate1") == "started"
    assert service.start_validator(request, "candidate2") is None
    assert len(starter.calls) == 1
    owner = starter.calls[0]["BOM_VALIDATOR_SERIAL_OWNER"]
    transition(storage, request, "candidate1", owner, "reserved", "running")
    assert service.start_validator(request, "candidate2") is None
    transition(storage, request, "candidate1", owner, "running", "idle")
    assert service.start_validator(request, "candidate2") == "started"


def test_dispatch_releases_writer_before_remote_start():
    storage, _, request, _ = planned()
    class InspectingStarter(Starter):
        def start(self, values):
            from bom_processor.package_writer_gate import candidate_writer
            with candidate_writer(storage, request):
                pass
            return super().start(values)
    assert CandidateAutomationService(storage, validator_starter=InspectingStarter()).start_validator(request, "c1") == "started"


def test_unknown_start_retains_slot():
    storage, _, request, _ = planned()
    class FailingStarter(Starter):
        def start(self, values):
            super().start(values)
            raise RuntimeError("unknown")
    starter = FailingStarter()
    service = CandidateAutomationService(storage, validator_starter=starter)
    with pytest.raises(RuntimeError):
        service.start_validator(request, "c1")
    assert service.start_validator(request, "c2") is None
    assert len(starter.calls) == 1


def test_existing_attempt_never_gets_an_orphan_slot():
    storage, _, request, _ = planned()
    from bom_processor.candidate_authoring import AUTHORING_VERSION
    path = f"candidate-automation/{request}/c1/validator/{AUTHORING_VERSION}/attempt-1.started.json"
    storage.registry[path] = b'{}'
    starter = Starter()
    service = CandidateAutomationService(storage, validator_starter=starter)
    assert service.start_validator(request, "c1") is None
    assert service.start_validator(request, "c2") == "started"
    assert len(starter.calls) == 1


def test_duplicate_worker_and_wrong_owner_cannot_release():
    storage, _, request, _ = planned()
    owner = reserve(storage, request, "c1")
    transition(storage, request, "c1", owner, "reserved", "running")
    with pytest.raises(SerialValidationHeld):
        transition(storage, request, "c1", owner, "reserved", "running")
    with pytest.raises(SerialValidationHeld):
        transition(storage, request, "c1", "0" * 64, "running", "idle")


def test_independent_packages_have_independent_slots():
    first, _, request1, _ = planned()
    second, _, request2, _ = planned()
    assert reserve(first, request1, "c1")
    assert reserve(second, request2, "c2")


@pytest.mark.parametrize("failure", [False, True])
def test_actual_validation_service_releases_only_terminal_outcome(failure):
    from test_candidate_validation import setup_validation
    from bom_processor.candidate_validation import CandidateValidationError
    storage, _, request, _ = planned()
    service, fixture_storage, worker, fixture_request, candidate = setup_validation()
    # Use real verified membership with the ordinary validation test fixture.
    from bom_processor.author_budget import _membership
    member_path = f"author-budget/v1/members/{request}.json"
    member = json.loads(storage.registry[member_path])
    member["request_id"] = fixture_request.request_id
    fixture_storage.registry.update(storage.registry)
    fixture_storage.registry[f"author-budget/v1/members/{fixture_request.request_id}.json"] = json.dumps(member).encode()
    assert _membership(fixture_storage, fixture_request.request_id)
    service.serial_owner = reserve(fixture_storage, fixture_request.request_id, candidate.candidate_id)
    from bom_processor.serial_validation import SerialValidationHeld
    intended_owner = service.serial_owner
    service.serial_owner = None
    with pytest.raises(SerialValidationHeld):
        service.validate()
    assert worker.calls == []
    service.serial_owner = intended_owner
    if failure:
        from test_candidate_validation import semantic_workbook
        worker.output = semantic_workbook([("UNKNOWN", "Bad", 1)])
        with pytest.raises(CandidateValidationError):
            service.validate()
    else:
        assert service.validate().status == "awaiting_approval"
    assert reserve(fixture_storage, fixture_request.request_id, "next")