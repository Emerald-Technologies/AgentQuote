"""Offline trusted membership and request-local author policy tests."""
import json

import pytest

from bom_processor.candidate_automation import CandidateAutomationService
from bom_processor.candidate_authoring import AUTHORING_VERSION
from test_author_budget_integration import planned, Starter, finish, NOW

QUOTE = "12345678-1234-4234-8234-123456789012"


def test_selected_author_and_refinement_use_v6_without_mutating_dispatcher():
    storage, _, request_id, _ = planned()
    starter = Starter()
    automation = CandidateAutomationService(storage, starter, association_quote_ids=frozenset({QUOTE}),
                                            author_budget_enabled=True, budget_clock=lambda: NOW)
    assert automation.start_author(request_id) == "started"
    assert starter.calls[-1]["BOM_SOURCE_ASSOCIATIONS"] == "true"
    assert automation.authoring_version == AUTHORING_VERSION
    assert automation.author_environment == {}
    finish(storage, starter.calls[-1], "completed", "candidate-A")
    assert automation.start_author_refinement(request_id, "candidate-A", {"failure_message": "bad"}) == "started"
    assert starter.calls[-1]["BOM_SOURCE_ASSOCIATIONS"] == "true"


def test_unlisted_member_uses_legacy_author_policy():
    storage, _, request_id, _ = planned()
    starter = Starter()
    automation = CandidateAutomationService(storage, starter,
        association_quote_ids=frozenset({"22345678-1234-4234-8234-123456789012"}), budget_clock=lambda: NOW)
    assert automation.start_author(request_id) == "started"
    assert starter.calls[-1]["BOM_SOURCE_ASSOCIATIONS"] == "false"


def test_existing_enrolled_v6_lineage_survives_allowlist_removal():
    storage, _, request_id, _ = planned()
    starter = Starter()
    automation = CandidateAutomationService(storage, starter, association_quote_ids=frozenset({QUOTE}), budget_clock=lambda: NOW)
    automation.start_author(request_id)
    finish(storage, starter.calls[-1], "completed", "candidate-A")
    storage.candidates[f"candidates/{request_id}/candidate-A/candidate-manifest.json"] = json.dumps({
        "request_id": request_id, "candidate_id": "candidate-A", "version": "candidate-authoring.v6"}).encode()
    cleared = CandidateAutomationService(storage, starter, budget_clock=lambda: NOW)
    assert cleared.start_author_refinement(request_id, "candidate-A", {}) == "started"
    assert starter.calls[-1]["BOM_SOURCE_ASSOCIATIONS"] == "true"


def test_config_requires_budget_scope_and_validates_unconditionally(monkeypatch):
    from deterministic_api import _association_quote_ids
    monkeypatch.setenv("BOM_SOURCE_ASSOCIATIONS", "false")
    monkeypatch.setenv("BOM_AUTHOR_GROUP_BUDGET", "false")
    monkeypatch.setenv("BOM_SOURCE_ASSOCIATIONS_QUOTE_UUIDS", QUOTE)
    monkeypatch.setenv("BOM_AUTHOR_GROUP_BUDGET_QUOTE_UUIDS", "")
    with pytest.raises(RuntimeError, match="requires budget enrollment"):
        _association_quote_ids()
    monkeypatch.setenv("BOM_AUTHOR_GROUP_BUDGET_QUOTE_UUIDS", QUOTE)
    assert _association_quote_ids() == frozenset({QUOTE})
    monkeypatch.setenv("BOM_SOURCE_ASSOCIATIONS", "true")
    monkeypatch.setenv("BOM_SOURCE_ASSOCIATIONS_QUOTE_UUIDS", "*")
    with pytest.raises(ValueError):
        _association_quote_ids()


@pytest.mark.parametrize("original_mode", [False, True])
def test_failed_before_manifest_keeps_saved_mode_when_config_changes(original_mode):
    storage, _, request_id, _ = planned()
    starter = Starter()
    initial = CandidateAutomationService(storage, starter, budget_clock=lambda: NOW,
        association_quote_ids=frozenset({QUOTE}) if original_mode else frozenset())
    assert initial.start_author(request_id) == "started"
    finish(storage, starter.calls[-1], "failed")
    assert not storage.list_candidate_paths(f"candidates/{request_id}/", 100)
    changed = CandidateAutomationService(storage, starter, budget_clock=lambda: NOW,
        association_quote_ids=frozenset() if original_mode else frozenset({QUOTE}))
    assert changed.start_author(request_id) == "started"
    assert starter.calls[-1]["BOM_SOURCE_ASSOCIATIONS"] == str(original_mode).lower()
    record = json.loads(storage.registry[f"author-policy/v1/requests/{request_id}.json"])
    assert record["source_associations"] is original_mode
    assert len(starter.calls) == 2


def test_policy_binding_corruption_blocks_before_dispatch():
    storage, _, request_id, _ = planned()
    starter = Starter()
    automation = CandidateAutomationService(storage, starter, budget_clock=lambda: NOW)
    automation.start_author(request_id)
    finish(storage, starter.calls[-1], "failed")
    path = f"author-policy/v1/requests/{request_id}.json"
    record = json.loads(storage.registry[path])
    record["membership_hash"] = "0" * 64
    storage.registry[path] = json.dumps(record).encode()
    with pytest.raises(Exception):
        automation.start_author(request_id)
    assert len(starter.calls) == 1


def test_existing_budget_history_without_policy_is_not_guessed():
    from bom_processor.author_budget import Budget
    storage, _, request_id, _ = planned()
    assert Budget(storage, request_id, clock=lambda: NOW).reserve("author")["status"] == "reserved"
    starter = Starter()
    with pytest.raises(Exception):
        CandidateAutomationService(storage, starter, budget_clock=lambda: NOW).start_author(request_id)
    assert not starter.calls


def test_first_policy_wins_conflicting_concurrent_configuration(monkeypatch):
    from bom_processor.author_policy import resolve
    from bom_processor.core import BlobConflictError
    storage, _, request_id, _ = planned()
    original = storage.upload_registry_immutable
    competing = False
    def upload(path, data, content_type):
        nonlocal competing
        if path.startswith("author-policy/") and not competing:
            competing = True
            rival = json.loads(data)
            rival.update(source_associations=False, authoring_version=AUTHORING_VERSION)
            original(path, json.dumps(rival).encode(), content_type)
            raise BlobConflictError("competing first choice")
        return original(path, data, content_type)
    monkeypatch.setattr(storage, "upload_registry_immutable", upload)
    assert resolve(storage, request_id, True)["source_associations"] is False