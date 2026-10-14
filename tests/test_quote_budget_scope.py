"""Offline scope/config and trusted enrollment regressions; no jobs or network."""
import pytest

from bom_processor.author_budget import Budget
from bom_processor.candidates import AggregateCandidateRequiredError
from bom_processor.quote_budget_scope import parse_quote_scope
from bom_processor.remote import RemoteBomProcessor
from bom_processor import InMemoryStorage, ProcessingRequest
from test_author_budget_integration import package

SELECTED = "12345678-1234-4234-8234-123456789012"
OTHER = "22345678-1234-4234-8234-123456789012"


def test_empty_scope_and_case_normalization():
    assert parse_quote_scope("  ") == frozenset()
    assert parse_quote_scope(f" {SELECTED.upper()}, {SELECTED} ") == frozenset({SELECTED})


@pytest.mark.parametrize("value", ["*", "not-a-uuid", SELECTED + ",", "," + SELECTED,
                                    ",".join([SELECTED] * 101), None])
def test_invalid_scope_fails_closed(value):
    with pytest.raises(ValueError, match="invalid author budget quote scope"):
        parse_quote_scope(value)


def test_only_selected_quote_enrolls_without_shared_state_leak():
    storage = InMemoryStorage()
    processor = RemoteBomProcessor(storage, None, author_budget_quote_ids=frozenset({SELECTED}))
    for quote in (OTHER, SELECTED, OTHER):
        original = package(storage)
        documents = []
        for source in original.documents:
            path = source.blob_path.replace(SELECTED, quote)
            storage.sources[path] = storage.sources[source.blob_path]
            documents.append({"document_id": source.document_id, "blob_path": path,
                              "size_bytes": source.size_bytes, "sha256": source.sha256})
        request = ProcessingRequest.from_dict({"job_id": f"scope-{quote[:8]}", "customer_key": "cust",
            "parser_key": "Parser1", "rfq_uuid": quote, "package_version": "1", "documents": documents})
        with pytest.raises(AggregateCandidateRequiredError) as error:
            processor.process(request)
        child = error.value.candidates[0].request_id
        state = Budget(storage, child).inspect()
        assert state["status"] == ("available" if quote == SELECTED else "legacy")
    assert processor.author_budget_enabled is False


def test_api_scope_helper_rejects_wildcard_and_defaults_empty(monkeypatch):
    from deterministic_api import _author_budget_quote_ids
    monkeypatch.delenv("BOM_AUTHOR_GROUP_BUDGET_QUOTE_UUIDS", raising=False)
    assert _author_budget_quote_ids() == frozenset()
    monkeypatch.setenv("BOM_AUTHOR_GROUP_BUDGET_QUOTE_UUIDS", "*")
    with pytest.raises(ValueError):
        _author_budget_quote_ids()


def test_persisted_membership_stays_budgeted_after_selector_removed():
    from bom_processor.candidate_automation import CandidateAutomationService
    from test_author_budget_integration import planned, Starter, finish, NOW
    storage, _, request_id, _ = planned()
    starter = Starter()
    enabled = CandidateAutomationService(storage, starter, author_budget_enabled=True, budget_clock=lambda: NOW)
    assert enabled.start_author(request_id) == "started"
    finish(storage, starter.calls[-1], "completed", "candidate-A")
    cleared = CandidateAutomationService(storage, starter, author_budget_enabled=False, budget_clock=lambda: NOW)
    assert cleared.start_author_refinement(request_id, "candidate-A", {"failure_message": "bad header"}) == "started"
    assert starter.calls[-1]["AUTHOR_BUDGET_SLOT"] == "2"
    assert cleared.start_author_refinement(request_id, "candidate-A", {}) == "budget_in_flight"
    assert len(starter.calls) == 2


@pytest.mark.parametrize("global_value", ["false", "true"])
def test_invalid_scope_fails_before_any_startup_dependency(monkeypatch, global_value):
    import deterministic_api as api
    monkeypatch.setenv("BOM_AUTHOR_GROUP_BUDGET", global_value)
    monkeypatch.setenv("BOM_AUTHOR_GROUP_BUDGET_QUOTE_UUIDS", "*")
    monkeypatch.setattr(api.ApiConfig, "from_env", lambda: pytest.fail("config/client setup reached"))
    monkeypatch.setattr(api, "ThreadingHTTPServer", lambda *args: pytest.fail("server reached"))
    with pytest.raises(ValueError, match="invalid author budget quote scope"):
        api.main()