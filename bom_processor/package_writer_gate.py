"""Serialize package eligibility writers through bounded conditional storage writes.

Inputs: verified package ID, operation and storage. Output: exclusive context.
No expiry or stealing: crashed owners remain held. Run tests/test_package_writer_gate.py.
"""
import json
import secrets
from contextlib import contextmanager, nullcontext

from .core import BlobNotFoundError, ProcessingError

VERSION = "package-writer-gate.v1"
OPERATIONS = frozenset({"recovery", "approval", "validation"})


class PackageWriterBusy(ProcessingError):
    def __init__(self):
        super().__init__("PACKAGE_WRITER_HELD")


class WriterGuard:
    def __init__(self):
        self.write_attempted = False
        self.write_completed = False
        self.safe_rejection = False

    def require(self, condition):
        from .author_budget import BudgetError
        if not condition:
            self.safe_rejection = not self.write_attempted
            raise BudgetError()

    def reject(self, exception):
        """Only explicit eligibility branches may classify a pre-write rejection."""
        self.safe_rejection = not self.write_attempted
        raise exception


def _read(storage, path):
    try:
        data, etag = storage.download_registry_with_etag(path, 4096)
    except BlobNotFoundError:
        return None, None
    value = json.loads(data)
    if (type(value) is not dict or set(value) != {"version", "plan_id", "state", "owner", "operation"}
            or value["version"] != VERSION or value["state"] not in {"idle", "held"}
            or type(value["owner"]) is not str or len(value["owner"]) != 64
            or value["operation"] not in OPERATIONS):
        raise PackageWriterBusy()
    return value, etag


@contextmanager
def package_writer(storage, plan_id, operation, *, request_id=None, candidate_id=None,
                   semantic_coordinator=None):
    if (type(plan_id) is not str or len(plan_id) != 64
            or any(char not in "0123456789abcdef" for char in plan_id)
            or operation not in OPERATIONS):
        raise PackageWriterBusy()
    path = f"packages/{plan_id}/coordination/writer.v1.json"
    try:
        prior, etag = _read(storage, path)
        if prior and (prior["plan_id"] != plan_id or prior["state"] != "idle"):
            raise PackageWriterBusy()
        record = {"version": VERSION, "plan_id": plan_id, "state": "held",
                  "owner": secrets.token_hex(32), "operation": operation}
        held_etag = storage.upload_registry_if_match(path, json.dumps(record, sort_keys=True).encode(),
                                                     "application/json", etag)
    except Exception:
        raise PackageWriterBusy() from None
    outcome = None
    guard = WriterGuard()
    guard.plan_id = plan_id
    guard.owner = record["owner"]
    guard.held_etag = held_etag
    # Definitive pre-action semantic rejection releases only this short writer.
    # It never alters or clears the persistent repair fence.
    from .semantic_repair import assert_action, SemanticRepairHeld
    try:
        if semantic_coordinator is None:
            assert_action(storage, plan_id, operation, request_id, candidate_id)
        else:
            # Internal coordinator checks the independently persisted SQL owner;
            # no route/caller supplies a bypass boolean or owner string here.
            from .semantic_repair import SemanticFenceCoordinator
            if not isinstance(semantic_coordinator, SemanticFenceCoordinator):
                raise SemanticRepairHeld()
            semantic_coordinator.authorize_writer(plan_id, operation)
    except SemanticRepairHeld:
        record["state"] = "idle"
        try:
            storage.upload_registry_if_match(path, json.dumps(record, sort_keys=True).encode(),
                                             "application/json", held_etag)
        except Exception:
            raise PackageWriterBusy() from None
        raise
    try:
        yield guard
    except BaseException as exception:
        # Unexpected failure or cancellation may have crossed a write/start boundary.
        # Preserve ownership until a separately approved inspection resolves it.
        from .author_budget import BudgetError
        validation_feedback = operation == "validation" and getattr(exception, "feedback_recorded", False) is True
        recovery_rejection = (operation == "recovery" and isinstance(exception, BudgetError)
                              and guard.safe_rejection and not guard.write_attempted)
        from .package_resolution import PackageResolutionError
        approval_rejection = (operation == "approval" and isinstance(exception, PackageResolutionError)
                      and guard.safe_rejection and not guard.write_attempted)
        if not (validation_feedback or recovery_rejection or approval_rejection):
            raise
        outcome = exception
    if operation == "recovery" and guard.write_attempted and not guard.write_completed:
        return
    try:
        record["state"] = "idle"
        storage.upload_registry_if_match(path, json.dumps(record, sort_keys=True).encode(),
                                         "application/json", held_etag)
    except Exception:
        raise PackageWriterBusy() from None
    if outcome is not None:
        raise outcome


def assert_plan_coordination(storage, plan):
    """Fail closed on alias plans until shared alias authority is explicitly supported."""
    from .author_budget import _membership
    for assignment in plan["assignments"]:
        if assignment.get("kind") != "candidate_request":
            continue
        binding = _membership(storage, assignment["request_id"])
        if binding and binding[0]["plan_id"] != plan["plan_id"]:
            raise PackageWriterBusy()


def candidate_writer(storage, request_id, operation="validation", *, candidate_id=None):
    """Only enrolled package requests participate; read errors are never absence."""
    from .author_budget import _membership
    member = _membership(storage, request_id)
    return package_writer(storage, member[0]["plan_id"], operation,
                          request_id=request_id, candidate_id=candidate_id) if member else nullcontext()