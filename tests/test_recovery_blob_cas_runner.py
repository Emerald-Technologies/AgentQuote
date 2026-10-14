"""Local-only approval, allowlist, and actual adapter CAS regression tests."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

RUNNER_PATH = Path(__file__).resolve().parents[1] / "dev/verify_recovery_blob_cas.py"
SPEC = importlib.util.spec_from_file_location("recovery_blob_cas_runner", RUNNER_PATH)
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


class FakeContainer:
    def __init__(self):
        self.blobs = {}
        self.lock = threading.RLock()
        self.successes = 0
        self.page_calls = 0

    def list_blobs(self, **kwargs):
        assert kwargs == {"name_starts_with": runner.PREFIX, "results_per_page": 2, "timeout": 30}
        container = self

        class Pages:
            def by_page(self):
                container.page_calls += 1
                yield [SimpleNamespace(name=path) for path in sorted(container.blobs)[:2]]
                raise AssertionError("second page forbidden")

        return Pages()

    def get_blob_client(self, path):
        runner.guard_path(path)
        container = self

        class Blob:
            def get_blob_properties(self, **kwargs):
                assert kwargs == {"timeout": 30}
                with container.lock:
                    data, etag = container.blobs[path]
                    return SimpleNamespace(size=len(data), etag=etag)

            def download_blob(self, **kwargs):
                assert kwargs["timeout"] == 30
                with container.lock:
                    data, etag = container.blobs[path]
                    if "etag" in kwargs:
                        assert kwargs["etag"] == etag
                    return SimpleNamespace(readall=lambda: data[:kwargs["length"]])

            def upload_blob(self, data, **kwargs):
                from azure.core.exceptions import ResourceExistsError, ResourceModifiedError
                assert kwargs["timeout"] == 30
                with container.lock:
                    existing = container.blobs.get(path)
                    if kwargs["overwrite"] is False and existing is not None:
                        raise ResourceExistsError("secret must never be reported", status_code=409)
                    if kwargs["overwrite"] is True and (existing is None or existing[1] != kwargs["etag"]):
                        raise ResourceModifiedError("secret must never be reported", status_code=412)
                    container.successes += 1
                    container.blobs[path] = (bytes(data), '"etag-' + str(container.successes) + '"')

        return Blob()


def local_clients(container=None):
    container = container or FakeContainer()
    budget = runner.Budget()
    clients = [runner.wrap_container(container, budget) for unused in range(2)]
    return clients, budget, container


def test_dry_run_never_loads_auth_or_clients(monkeypatch, capsys):
    def forbidden():
        pytest.fail("dry-run attempted authentication/client query")
    monkeypatch.setattr(runner, "load_live_clients", forbidden)
    monkeypatch.setattr(runner.subprocess, "run", forbidden)
    assert runner.main([]) == 0
    packet = json.loads(capsys.readouterr().out)
    assert packet["code"] == "DRY_RUN_ONLY"
    assert packet["plan"]["paths"] == list(runner.PATHS)
    assert packet["plan"]["sdk_total_requests_max"] == 21


@pytest.mark.parametrize("approval", [None, "", "0" * 64])
def test_missing_or_wrong_hash_never_loads_auth(monkeypatch, capsys, approval):
    def forbidden():
        pytest.fail("unapproved execution queried authentication")
    monkeypatch.setattr(runner, "load_live_clients", forbidden)
    monkeypatch.setattr(runner.subprocess, "run", forbidden)
    args = ["--execute"]
    if approval is not None:
        args.extend(["--approved-plan-sha256", approval])
    assert runner.main(args) == 2
    assert json.loads(capsys.readouterr().out) == {"code": "EXACT_SOURCE_APPROVAL_REQUIRED"}


def test_packet_deterministic_and_binds_both_sources_and_plan(monkeypatch):
    packet = runner.approval_packet()
    assert packet == runner.approval_packet()
    expected = runner.hashlib.sha256(runner.canonical_bytes(
        {key: packet[key] for key in ("plan", "source_sha256")})).hexdigest()
    assert expected == packet["approved_plan_sha256"]
    assert runner.approval_packet(runner_source=b"changed")["approved_plan_sha256"] != expected
    assert runner.approval_packet(adapter_source=b"changed")["approved_plan_sha256"] != expected
    original = runner.fixed_plan
    monkeypatch.setattr(runner, "fixed_plan", lambda: {**original(), "version": 2})
    assert runner.approval_packet()["approved_plan_sha256"] != expected


@pytest.mark.parametrize("path", ["candidates/live.json", "packages/live.json", "budgets/live.json",
                                  runner.PREFIX + "other.json", runner.PREFIX + "../create-race.json",
                                  "/" + runner.PATHS[0], runner.PATHS[0].replace("/", "\\")])
def test_exact_path_allowlist_blocks_all_io(path):
    clients, budget, container = local_clients()
    with pytest.raises(runner.SafeFailure, match="PATH_FORBIDDEN"):
        clients[0].download_registry_with_etag(path, 4096)
    with pytest.raises(runner.SafeFailure, match="PATH_FORBIDDEN"):
        clients[0].upload_registry_if_match(path, b"{}", "application/json", None)
    with pytest.raises(runner.SafeFailure, match="PATH_FORBIDDEN"):
        clients[0].adapter._parsers.get_blob_client(path)
    assert not any(budget.counts.values())
    assert container.blobs == {}


def test_forbidden_list_and_record_limits():
    clients, budget, container = local_clients()
    for prefix in ("", "candidates/", runner.PREFIX + "nested/"):
        with pytest.raises(runner.SafeFailure, match="PREFIX_FORBIDDEN"):
            clients[0].list_registry_paths(prefix, 1)
        with pytest.raises(runner.SafeFailure, match="PREFIX_FORBIDDEN"):
            clients[0].adapter._parsers.list_blobs(name_starts_with=prefix)
    with pytest.raises(runner.SafeFailure, match="RECORD_BOUND_FORBIDDEN"):
        clients[0].upload_registry_if_match(runner.PATHS[0], b"x" * 4097, "application/json", None)
    assert not any(budget.counts.values())


def test_two_native_adapter_clients_cas_and_ack_loss_retain_bytes():
    clients, budget, container = local_clients()
    assert clients[0].adapter is not clients[1].adapter
    result = runner.verify(clients, budget)
    assert result["counts"] == runner.fixed_plan()["expected"]
    assert result["sdk_requests"] == {"writes": 6, "properties": 9, "downloads": 5, "lists": 1}
    assert container.successes == 4
    assert container.page_calls == 1
    assert set(container.blobs) == set(runner.PATHS)
    assert container.blobs[runner.PATHS[1]][0] == runner.record("held-owner", "held")
    assert json.loads(container.blobs[runner.PATHS[0]][0])["owner"] in ("owner-0", "owner-1")


def test_existing_prefix_stops_without_writes():
    container = FakeContainer()
    container.blobs[runner.PREFIX + "already-present"] = (b"untouched", "etag")
    clients, budget, container = local_clients(container)
    with pytest.raises(runner.SafeFailure, match="PREFIX_NOT_EMPTY"):
        runner.verify(clients, budget)
    assert budget.counts == {"writes": 0, "properties": 0, "downloads": 0, "lists": 1}
    assert container.blobs[runner.PREFIX + "already-present"][0] == b"untouched"


def test_correct_approval_reports_only_safe_failure(monkeypatch, capsys):
    def failure():
        raise RuntimeError("raw Azure exception or token must not appear")
    monkeypatch.setattr(runner, "load_live_clients", failure)
    assert runner.main(["--execute", "--approved-plan-sha256",
                        runner.approval_packet()["approved_plan_sha256"]]) == 2
    assert json.loads(capsys.readouterr().out) == {"code": "VERIFICATION_STOPPED"}


def test_request_budget_no_retry():
    budget = runner.Budget()
    for unused in range(6):
        budget.take("writes")
    with pytest.raises(runner.SafeFailure, match="REQUEST_BOUND_EXCEEDED"):
        budget.take("writes")
    assert budget.counts["writes"] == 6


def test_empty_first_page_with_continuation_cannot_prove_absence():
    class PartialContainer(FakeContainer):
        def list_blobs(self, **kwargs):
            class Pages:
                def by_page(self):
                    class Iterator:
                        continuation_token = "more-entries"

                        def __iter__(self):
                            return self

                        def __next__(self):
                            return iter(())

                    return Iterator()

            return Pages()

    clients, budget, container = local_clients(PartialContainer())
    with pytest.raises(runner.SafeFailure, match="PREFIX_ABSENCE_UNCONFIRMED"):
        runner.verify(clients, budget)
    assert budget.counts["writes"] == 0


def test_unexpected_post_write_exception_stops_and_retains_bytes(monkeypatch, capsys):
    clients, budget, container = local_clients()
    original = clients[0].upload_registry_if_match

    def lose_actual_ack(path, data, content_type, etag):
        result = original(path, data, content_type, etag)
        if data == runner.record("held-owner", "held"):
            raise RuntimeError("unknown write outcome, sensitive raw detail")
        return result

    monkeypatch.setattr(clients[0], "upload_registry_if_match", lose_actual_ack)
    monkeypatch.setattr(runner, "load_live_clients", lambda: (clients, budget, []))
    assert runner.main(["--execute", "--approved-plan-sha256",
                        runner.approval_packet()["approved_plan_sha256"]]) == 2
    assert json.loads(capsys.readouterr().out) == {"code": "VERIFICATION_STOPPED"}
    assert budget.counts["writes"] == 6
    assert budget.counts["downloads"] == 4
    assert container.successes == 4
    assert container.blobs[runner.PATHS[1]][0] == runner.record("held-owner", "held")


def test_source_changed_after_approval_never_loads_auth(monkeypatch, capsys):
    approved = runner.approval_packet()["approved_plan_sha256"]
    original = runner.approval_packet
    monkeypatch.setattr(runner, "approval_packet", lambda: original(adapter_source=b"different source"))

    def forbidden():
        pytest.fail("stale source approval queried authentication")

    monkeypatch.setattr(runner, "load_live_clients", forbidden)
    assert runner.main(["--execute", "--approved-plan-sha256", approved]) == 2
    assert json.loads(capsys.readouterr().out) == {"code": "EXACT_SOURCE_APPROVAL_REQUIRED"}