"""Pin a fresh enrolled task's authoring mode before dispatch; no model calls.

Inputs: verified budget membership and proposed server-selected mode. Output:
immutable request policy. Run tests/test_quote_association_scope.py offline.
"""
from .author_budget import BudgetError, _hash, _membership, _read, _bytes, _require
from .core import BlobConflictError
from .candidate_authoring import AUTHORING_VERSION, ASSOCIATION_AUTHORING_VERSION

VERSION = "author-policy.v1"


def resolve(storage, request_id: str, selected: bool) -> dict:
    _require(type(selected) is bool)
    binding = _membership(storage, request_id)
    _require(binding is not None)
    member, _ = binding
    path = f"author-policy/v1/requests/{request_id}.json"
    base = {"version": VERSION, "request_id": request_id,
            "membership_hash": _hash(member), "group_id": member["group_id"]}

    def verify(record):
        _require(isinstance(record, dict) and set(record) == set(base) | {"source_associations", "authoring_version"})
        _require(all(record[key] == value for key, value in base.items()))
        mode = record["source_associations"]
        _require(type(mode) is bool and record["authoring_version"] ==
                 (ASSOCIATION_AUTHORING_VERSION if mode else AUTHORING_VERSION))
        return record

    existing = _read(storage, path)
    if existing is not None:
        return verify(existing)
    # Missing policy is only enrollable before ANY author reservation/history.
    _require(not storage.list_registry_paths(f"author-budget/v1/groups/{member['group_id']}/slots/", 100))
    _require(not storage.list_registry_paths(f"candidate-automation/{request_id}/", 100))
    _require(not storage.list_candidate_paths(f"candidates/{request_id}/", 100))
    proposed = {**base, "source_associations": selected,
                "authoring_version": ASSOCIATION_AUTHORING_VERSION if selected else AUTHORING_VERSION}
    try:
        storage.upload_registry_immutable(path, _bytes(proposed), "application/json")
    except BlobConflictError:
        # The first verified choice wins; a racing configuration never replaces it.
        return verify(_read(storage, path))
    return verify(_read(storage, path))