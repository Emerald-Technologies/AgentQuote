"""Offline recovery reservation/ownership checks; no authority or live dispatch."""
import copy
import threading
from concurrent.futures import ThreadPoolExecutor

from bom_processor.author_recovery import RecoveryService, authorization_path, reserve_recovery
from test_author_budget import digest, encoded, finish, ready


def failed_refinement():
    storage, budget, member = ready()
    first = budget.reserve("author")
    budget.mark_started(first, "initial-execution")
    finish(storage, first, "completed", "original-candidate")
    failed = budget.reserve("author-refinement", "original-candidate", "b" * 64)
    budget.mark_started(failed, "failed-execution")
    terminal = finish(storage, failed)
    body = {"version": "author-recovery-authorization.v1", "request_id": budget.request_id,
            "group_id": member["group_id"], "plan_id": member["plan_id"], "failed_slot": 2,
            "failed_reservation_hash": failed["reservation_hash"], "failed_terminal_hash": digest(terminal),
            "next_slot": 3, "source_candidate": "original-candidate", "failure_hash": "b" * 64,
            "actor_id": "verified-admin", "caller_application": "trusted-app",
            "reason": "Approved diagnostic recovery", "idempotency_key": "recovery-one"}
    authorization = {**body, "authorization_hash": digest(body)}
    storage.registry[authorization_path(member["group_id"], 2)] = encoded(authorization)
    return storage, budget, authorization


def test_recovery_consumes_next_slot_and_preserves_prior_bytes():
    storage, budget, authorization = failed_refinement()
    before = dict(storage.registry)
    assert budget.reserve("author-refinement", "original-candidate", "b" * 64)["reason"] == "budget_no_progress"
    claim = reserve_recovery(budget, authorization)
    assert claim["status"] == "reserved" and claim["slot"] == 3
    assert all(storage.registry[path] == value for path, value in before.items())
    budget.mark_dispatch(claim)
    budget.mark_started(claim, "recovery-execution")
    finish(storage, claim, "completed", "replacement-candidate")
    assert budget.inspect()["used"] == 3
    assert budget.selected_candidate() == "replacement-candidate"
    assert reserve_recovery(budget, authorization)["reason"] == "budget_recovery_consumed"


def test_unpersisted_or_tampered_authority_holds_without_consumption():
    storage, budget, authorization = failed_refinement()
    for field, value in (("actor_id", "other-admin"), ("next_slot", 4), ("failed_terminal_hash", "c" * 64)):
        changed = {**authorization, field: value}
        assert reserve_recovery(budget, changed)["reason"] == "budget_invalid"
    assert budget.inspect()["used"] == 2


def test_two_controllers_win_only_one_next_slot():
    storage, budget, authorization = failed_refinement()
    storage.race = threading.Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: reserve_recovery(budget, copy.deepcopy(authorization)), range(2)))
    assert sum(item["status"] == "reserved" for item in outcomes) == 1
    assert budget.inspect()["used"] == 3
    storage.race = None
    assert reserve_recovery(budget, authorization)["reason"] == "budget_recovery_consumed"


def test_crash_after_claim_holds_and_never_advances_slot():
    storage, budget, authorization = failed_refinement()
    assert reserve_recovery(budget, authorization)["status"] == "reserved"
    assert reserve_recovery(budget, authorization)["reason"] == "budget_recovery_consumed"
    assert budget.inspect()["used"] == 3
    assert budget.reserve("author-refinement", "original-candidate", "b" * 64)["reason"] == "budget_in_flight"


def service_setup(monkeypatch, *, fail_start=False):
    from types import SimpleNamespace
    storage, budget, authority = failed_refinement()
    del storage.registry[authorization_path(authority["group_id"], 2)]
    feedback = {"repairable": True, "failure": "TypeError", "failure_message": "bounded feedback"}
    # Real persisted budget history; isolated doubles for reviewed plan/feedback adapters.
    import json
    claim_path = f"author-budget/v1/groups/{authority['group_id']}/slots/2.claim.json"
    prior = json.loads(storage.registry[claim_path])
    prior["failure_hash"] = digest(feedback)
    prior["reservation_hash"] = digest({key: value for key, value in prior.items() if key != "reservation_hash"})
    storage.registry[claim_path] = encoded(prior)
    for kind in ("started", "execution", "terminal"):
        path = f"author-budget/v1/groups/{authority['group_id']}/slots/2.{kind}.json"
        marker = json.loads(storage.registry[path]); marker["reservation_hash"] = prior["reservation_hash"]
        storage.registry[path] = encoded(marker)
    terminal = json.loads(storage.registry[f"author-budget/v1/groups/{authority['group_id']}/slots/2.terminal.json"])
    value = {key: authority[key] for key in ("request_id", "group_id", "plan_id", "failed_slot", "source_candidate", "idempotency_key", "reason")}
    value.update(failure_hash=digest(feedback), failed_reservation_hash=prior["reservation_hash"], failed_terminal_hash=digest(terminal))
    monkeypatch.setattr("bom_processor.package_resolution.ParentPackageService._plan", lambda *_: {"assignments": [{"request_id": value["request_id"]}]})
    monkeypatch.setattr("bom_processor.candidate_status.CandidateStatusService.get", lambda *_: {"candidate_id": value["source_candidate"], "status": "generated_unvalidated"})
    monkeypatch.setattr("bom_processor.candidate_status.CandidateStatusService._latest_validation_failure", lambda *_: feedback)
    calls = []
    class Starter:
        def start(self, environment):
            calls.append(environment)
            if fail_start: raise RuntimeError("not retained")
            return "recovery-execution"
    scoped = SimpleNamespace(author_starter=Starter(), author_environment={"BOM_SOURCE_ASSOCIATIONS": "true"})
    automation = SimpleNamespace(_request_policy=lambda _: scoped)
    service = RecoveryService(storage, automation, clock=budget.clock)
    storage.candidates[f"candidates/{value['request_id']}/{value['source_candidate']}/candidate-manifest.json"] = b"{}"
    return storage, budget, service, value, calls


def test_service_dispatches_once_and_replay_never_starts(monkeypatch):
    _, budget, service, value, calls = service_setup(monkeypatch)
    result = service.recover(value, actor_id="verified-admin", caller_application="trusted-app")
    assert result == {"status": "started", "request_id": "request-1", "slot": 3, "execution": "recovery-execution"}
    replay = service.recover(value, actor_id="verified-admin", caller_application="trusted-app")
    assert replay["reason"] == "budget_recovery_consumed"
    assert replay["outcome"] == "started" and replay["execution"] == "recovery-execution"
    assert len(calls) == 1 and calls[0]["AUTHOR_BUDGET_SLOT"] == "3"
    assert calls[0]["AUTHOR_FEEDBACK"] == "Previous parser failed with TypeError: bounded feedback"
    assert budget.inspect()["used"] == 3


def test_replay_reports_success_after_candidate_changes(monkeypatch):
    import json
    storage, budget, service, value, calls = service_setup(monkeypatch)
    service.recover(value, actor_id="verified-admin", caller_application="trusted-app")
    path = f"author-budget/v1/groups/{value['group_id']}/slots/3.claim.json"
    claim = {"status": "reserved", **json.loads(storage.registry[path])}
    finish(storage, claim, "completed", "new-candidate")
    monkeypatch.setattr("bom_processor.candidate_status.CandidateStatusService.get", lambda *_: {"candidate_id": "new-candidate", "status": "approved"})
    replay = service.recover(value, actor_id="verified-admin", caller_application="trusted-app")
    assert replay["outcome"] == "completed" and replay["candidate_id"] == "new-candidate"
    assert len(calls) == 1


def test_failed_recovery_cannot_authorize_recursive_recovery(monkeypatch):
    import json
    import pytest
    storage, budget, service, value, calls = service_setup(monkeypatch)
    service.recover(value, actor_id="verified-admin", caller_application="trusted-app")
    prefix = f"author-budget/v1/groups/{value['group_id']}/slots/"
    claim = {"status": "reserved", **json.loads(storage.registry[prefix + "3.claim.json"])}
    terminal = finish(storage, claim)
    recursive = {**value, "failed_slot": 3, "failed_reservation_hash": claim["reservation_hash"],
                 "failed_terminal_hash": digest(terminal), "idempotency_key": "recursive"}
    with pytest.raises(Exception):
        service.recover(recursive, actor_id="verified-admin", caller_application="trusted-app")
    assert len(calls) == 1 and budget.inspect()["used"] == 3
    assert authorization_path(value["group_id"], 3) not in storage.registry


def test_service_unknown_start_preserves_single_counted_claim(monkeypatch):
    import pytest
    from bom_processor.package_writer_gate import PackageWriterBusy
    _, budget, service, value, calls = service_setup(monkeypatch, fail_start=True)
    assert service.recover(value, actor_id="verified-admin", caller_application="trusted-app")["reason"] == "budget_start_unknown"
    with pytest.raises(PackageWriterBusy):
        service.recover(value, actor_id="verified-admin", caller_application="trusted-app")
    assert service.lookup(value, actor_id="verified-admin", caller_application="trusted-app")["outcome"] == "start_unknown"
    assert len(calls) == 1
    assert budget.inspect()["reason"] == "budget_start_unknown"


def test_service_rejects_approved_package_before_authorization(monkeypatch):
    import pytest
    storage, budget, service, value, calls = service_setup(monkeypatch)
    storage.registry[f"packages/{value['plan_id']}/approvals/approval.json"] = b"{}"
    with pytest.raises(Exception):
        service.recover(value, actor_id="verified-admin", caller_application="trusted-app")
    assert calls == [] and budget.inspect()["used"] == 2
    assert authorization_path(value["group_id"], 2) not in storage.registry


def test_rejected_inputs_do_not_block_valid_recovery(monkeypatch):
    import json
    import pytest
    from bom_processor.author_budget import BudgetError
    storage, _, service, value, calls = service_setup(monkeypatch)
    gate = f"packages/{value['plan_id']}/coordination/writer.v1.json"
    for changed in ({**value, "extra": True}, {**value, "failed_slot": True},
                    {**value, "failed_terminal_hash": "c" * 64}):
        with pytest.raises(BudgetError):
            service.recover(changed, actor_id="verified-admin", caller_application="trusted-app")
        assert gate not in storage.registry or json.loads(storage.registry[gate])["state"] == "idle"
    assert service.recover(value, actor_id="verified-admin", caller_application="trusted-app")["status"] == "started"
    with pytest.raises(BudgetError):
        service.recover({**value, "idempotency_key": "different"}, actor_id="verified-admin", caller_application="trusted-app")
    assert json.loads(storage.registry[gate])["state"] == "idle"
    assert service.recover(value, actor_id="verified-admin", caller_application="trusted-app")["reason"] == "budget_recovery_consumed"
    assert len(calls) == 1


def test_authority_write_attempt_throwing_keeps_gate_held(monkeypatch):
    import json
    import pytest
    from bom_processor.author_budget import BudgetError
    storage, _, service, value, calls = service_setup(monkeypatch)
    def reject_write(*args):
        raise BudgetError()
    monkeypatch.setattr(storage, "upload_registry_immutable", reject_write)
    with pytest.raises(BudgetError):
        service.recover(value, actor_id="verified-admin", caller_application="trusted-app")
    assert json.loads(storage.registry[f"packages/{value['plan_id']}/coordination/writer.v1.json"])["state"] == "held"
    assert calls == []


def test_extra_published_manifest_prevents_recovery_even_with_old_selected_candidate(monkeypatch):
    import pytest
    from bom_processor.author_budget import BudgetError
    storage, budget, service, value, calls = service_setup(monkeypatch)
    storage.candidates[f"candidates/{value['request_id']}/unaccounted-candidate/candidate-manifest.json"] = b"published"
    before = dict(storage.candidates)
    with pytest.raises(BudgetError):
        service.recover(value, actor_id="verified-admin", caller_application="trusted-app")
    assert calls == [] and budget.inspect()["used"] == 2
    assert authorization_path(value["group_id"], 2) not in storage.registry
    assert storage.candidates == before


def test_lookup_while_gate_held_and_package_advanced_is_byte_read_only(monkeypatch):
    import json
    import pytest
    from bom_processor.author_budget import BudgetError
    storage, _, service, value, calls = service_setup(monkeypatch)
    service.recover(value, actor_id="verified-admin", caller_application="trusted-app")
    claim = {"status": "reserved", **json.loads(storage.registry[f"author-budget/v1/groups/{value['group_id']}/slots/3.claim.json"])}
    finish(storage, claim, "completed", "new-candidate")
    storage.registry[f"packages/{value['plan_id']}/approvals/approval.json"] = b"advanced"
    gate_path = f"packages/{value['plan_id']}/coordination/writer.v1.json"
    gate = json.loads(storage.registry[gate_path]); gate["state"] = "held"
    storage.registry[gate_path] = encoded(gate)
    def forbidden(*args, **kwargs):
        pytest.fail("lookup must not invoke mutable eligibility or writes")
    monkeypatch.setattr(storage, "upload_registry_if_match", forbidden)
    monkeypatch.setattr(storage, "upload_registry_immutable", forbidden)
    monkeypatch.setattr("bom_processor.package_resolution.ParentPackageService._plan", forbidden)
    monkeypatch.setattr("bom_processor.candidate_status.CandidateStatusService.get", forbidden)
    monkeypatch.setattr(service.automation, "_request_policy", forbidden)
    before = (dict(storage.registry), dict(storage.candidates))
    result = service.lookup(value, actor_id="verified-admin", caller_application="trusted-app")
    assert result["outcome"] == "completed" and result["candidate_id"] == "new-candidate"
    for changed, actor, caller in (({**value, "reason": "different"}, "verified-admin", "trusted-app"),
                                   (value, "different-admin", "trusted-app"),
                                   (value, "verified-admin", "different-app")):
        with pytest.raises(BudgetError):
            service.lookup(changed, actor_id=actor, caller_application=caller)
    assert (storage.registry, storage.candidates) == before
    assert len(calls) == 1


def test_lookup_missing_authority_is_unavailable_without_writes(monkeypatch):
    import pytest
    from bom_processor.author_budget import BudgetError
    storage, _, service, value, calls = service_setup(monkeypatch)
    before = dict(storage.registry)
    with pytest.raises(BudgetError):
        service.lookup(value, actor_id="verified-admin", caller_application="trusted-app")
    assert storage.registry == before and calls == []


def test_manifest_listing_contract_fails_closed_and_releases_expected_rejections(monkeypatch):
    import json
    import pytest
    from bom_processor.author_budget import BudgetError
    storage, budget, service, value, calls = service_setup(monkeypatch)
    root = f"candidates/{value['request_id']}/"
    real_listing = storage.list_candidate_paths
    cases = (None, [root + "../extra/candidate-manifest.json"],
             ["candidates/other/extra/candidate-manifest.json"],
             [root + "bad/name/candidate-manifest.json"],
             [root + "bad!id/candidate-manifest.json"],
             [root + "original-candidate/candidate-manifest.json"] * 2,
             [root + f"extra-{index}/candidate-manifest.json" for index in range(100)])
    for paths in cases:
        monkeypatch.setattr(storage, "list_candidate_paths", lambda *_, paths=paths: paths)
        with pytest.raises(BudgetError):
            service.recover(value, actor_id="verified-admin", caller_application="trusted-app")
        assert json.loads(storage.registry[f"packages/{value['plan_id']}/coordination/writer.v1.json"])["state"] == "idle"
    assert calls == [] and budget.inspect()["used"] == 2
    monkeypatch.setattr(storage, "list_candidate_paths", real_listing)
    assert service.recover(value, actor_id="verified-admin", caller_application="trusted-app")["status"] == "started"


def test_unexpected_prewrite_storage_errors_remain_held(monkeypatch):
    import json
    import pytest
    from bom_processor.author_budget import BudgetError
    for method, exception in (("list_candidate_paths", RuntimeError("listing unavailable")),
                              ("download_registry", BudgetError())):
        storage, _, service, value, calls = service_setup(monkeypatch)
        original = getattr(storage, method)
        def fail(*args, original=original, exception=exception, **kwargs):
            if method == "download_registry" and args[0].endswith("writer.v1.json"):
                return original(*args, **kwargs)
            raise exception
        monkeypatch.setattr(storage, method, fail)
        with pytest.raises(type(exception)):
            service.recover(value, actor_id="verified-admin", caller_application="trusted-app")
        assert json.loads(storage.registry[f"packages/{value['plan_id']}/coordination/writer.v1.json"])["state"] == "held"
        assert calls == []


def test_reservation_write_failure_returning_hold_keeps_gate_held(monkeypatch):
    import json
    storage, budget, service, value, calls = service_setup(monkeypatch)
    original = storage.upload_registry_immutable
    def fail_claim(path, *args):
        if path.endswith("3.claim.json"):
            raise RuntimeError("ambiguous reservation write")
        return original(path, *args)
    monkeypatch.setattr(storage, "upload_registry_immutable", fail_claim)
    assert service.recover(value, actor_id="verified-admin", caller_application="trusted-app")["reason"] == "budget_invalid"
    assert json.loads(storage.registry[f"packages/{value['plan_id']}/coordination/writer.v1.json"])["state"] == "held"
    assert calls == [] and budget.inspect()["used"] == 2


def test_exact_syntax_actor_and_caller_checked_before_gate(monkeypatch):
    import pytest
    from bom_processor.author_budget import BudgetError
    storage, _, service, value, calls = service_setup(monkeypatch)
    def no_gate(*args, **kwargs):
        pytest.fail("invalid syntax must not acquire the gate")
    monkeypatch.setattr(storage, "upload_registry_if_match", no_gate)
    cases = [(value, "invalid actor", "trusted-app"), (value, "verified-admin", "invalid caller")]
    for field, changed in (("request_id", "../bad"), ("source_candidate", "bad/path"),
                           ("idempotency_key", ""), ("group_id", "bad"), ("plan_id", "bad"),
                           ("failed_reservation_hash", "bad"), ("failed_terminal_hash", "bad"),
                           ("failure_hash", "bad"), ("failed_slot", 6), ("reason", " " * 501)):
        cases.append(({**value, field: changed}, "verified-admin", "trusted-app"))
    for changed, actor, caller in cases:
        with pytest.raises(BudgetError):
            service.recover(changed, actor_id=actor, caller_application=caller)
    assert calls == []


def test_published_write_then_exception_never_releases_or_restarts(monkeypatch):
    import json
    import pytest
    from bom_processor.author_budget import BudgetError
    from bom_processor.package_writer_gate import PackageWriterBusy
    for boundary in ("2.authorization.json", "3.claim.json", "3.dispatch.json", "3.started.json"):
        storage, _, service, value, calls = service_setup(monkeypatch)
        before = dict(storage.registry)
        original = storage.upload_registry_immutable
        published = []
        def publish_then_fail(path, *args, original=original, boundary=boundary):
            result = original(path, *args)
            if path.endswith(boundary):
                published.append(path)
                raise RuntimeError("write acknowledgement lost")
            return result
        monkeypatch.setattr(storage, "upload_registry_immutable", publish_then_fail)
        if boundary in {"2.authorization.json", "3.dispatch.json"}:
            with pytest.raises(RuntimeError if boundary == "2.authorization.json" else BudgetError):
                service.recover(value, actor_id="verified-admin", caller_application="trusted-app")
        else:
            assert service.recover(value, actor_id="verified-admin", caller_application="trusted-app")["status"] == "hold"
        assert len(published) == 1 and published[0] in storage.registry
        assert all(storage.registry[path] == data for path, data in before.items())
        assert json.loads(storage.registry[f"packages/{value['plan_id']}/coordination/writer.v1.json"])["state"] == "held"
        with pytest.raises(PackageWriterBusy):
            service.recover(value, actor_id="verified-admin", caller_application="trusted-app")
        assert len(calls) == (1 if boundary == "3.started.json" else 0)