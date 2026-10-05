"""Deterministic writer exclusion and fail-closed crash/ownership tests."""
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from bom_processor.package_writer_gate import PackageWriterBusy, package_writer
from test_author_budget import Storage

PLAN = "a" * 64


def test_approval_and_validation_cannot_cross_recovery_dispatch():
    store = Storage()
    with package_writer(store, PLAN, "recovery"):
        for operation in ("approval", "validation", "recovery"):
            with pytest.raises(PackageWriterBusy):
                with package_writer(store, PLAN, operation):
                    pytest.fail("competing writer entered")
    with package_writer(store, PLAN, "approval"):
        pass


def test_crashed_writer_remains_held_without_stealing():
    store = Storage()
    with pytest.raises(RuntimeError):
        with package_writer(store, PLAN, "recovery"):
            raise RuntimeError("simulate crash after a stateful boundary")
    with pytest.raises(PackageWriterBusy):
        with package_writer(store, PLAN, "approval"):
            pass


def test_two_simultaneous_writers_have_one_owner():
    store = Storage()
    entered, release = threading.Event(), threading.Event()
    def owner():
        with package_writer(store, PLAN, "recovery"):
            entered.set()
            assert release.wait(5)
    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(owner)
        assert entered.wait(5)
        with pytest.raises(PackageWriterBusy):
            with package_writer(store, PLAN, "validation"):
                pass
        release.set()
        future.result()


def test_malformed_or_wrong_package_gate_fails_closed():
    store = Storage()
    path = f"packages/{PLAN}/coordination/writer.v1.json"
    store.registry[path] = b"{}"
    with pytest.raises(PackageWriterBusy):
        with package_writer(store, PLAN, "recovery"):
            pass


def test_known_recorded_validation_failure_releases_but_unrecorded_holds():
    store = Storage()
    error = RuntimeError("known terminal failure")
    error.feedback_recorded = True
    with pytest.raises(RuntimeError):
        with package_writer(store, PLAN, "validation"):
            raise error
    with package_writer(store, PLAN, "recovery"):
        pass
    with pytest.raises(RuntimeError):
        with package_writer(store, PLAN, "validation"):
            raise RuntimeError("feedback publication failed")
    with pytest.raises(PackageWriterBusy):
        with package_writer(store, PLAN, "recovery"):
            pass


def test_real_in_memory_storage_has_single_conditional_owner():
    from bom_processor.core import InMemoryStorage, BlobConflictError
    store = InMemoryStorage()
    barrier = threading.Barrier(2)
    def claim(index):
        barrier.wait(5)
        try:
            store.upload_registry_if_match("gate", str(index).encode(), "application/json", None)
            return True
        except BlobConflictError:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(claim, (1, 2))) == 1


def test_alias_plan_is_rejected_before_writer_acquisition():
    from bom_processor.package_writer_gate import assert_plan_coordination
    from test_author_budget import ready
    store, _, member = ready()
    alias = {"plan_id": "f" * 64, "assignments": [{"kind": "candidate_request", "request_id": "request-1"}]}
    with pytest.raises(PackageWriterBusy):
        assert_plan_coordination(store, alias)
    assert_plan_coordination(store, {**alias, "plan_id": member["plan_id"]})