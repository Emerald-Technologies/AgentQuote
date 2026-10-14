"""Reserve one asynchronous validator per enrolled package; never expire ownership.

Inputs: verified budget membership, request/candidate and storage conditional writes.
Output: owned slot transitions. Run tests/test_serial_validation.py offline.
"""
import json
import re
import secrets

from .author_budget import _membership
from .core import BlobNotFoundError, ProcessingError

VERSION = "package-serial-validation.v1"


class SerialValidationHeld(ProcessingError):
    def __init__(self):
        super().__init__("PACKAGE_VALIDATOR_HELD")


def _path(storage, request_id):
    binding = _membership(storage, request_id)
    if binding is None:
        return None
    return f"packages/{binding[0]['plan_id']}/coordination/validator.v1.json"


def _read(storage, path):
    try:
        raw, etag = storage.download_registry_with_etag(path, 4096)
    except BlobNotFoundError:
        return None, None
    record = json.loads(raw)
    if (not isinstance(record, dict) or set(record) != {"version", "state", "owner", "request_id", "candidate_id"}
            or record["version"] != VERSION or record["state"] not in {"reserved", "running", "idle"}
            or not isinstance(record["owner"], str) or not re.fullmatch(r"[0-9a-f]{64}", record["owner"])
            or any(not isinstance(record[key], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", record[key])
                   for key in ("request_id", "candidate_id"))):
        raise SerialValidationHeld()
    return record, etag


def reserve(storage, request_id, candidate_id):
    path = _path(storage, request_id)
    if path is None:
        return None
    prior, etag = _read(storage, path)
    if prior is not None and prior["state"] != "idle":
        raise SerialValidationHeld()
    record = {"version": VERSION, "state": "reserved", "owner": secrets.token_hex(32),
              "request_id": request_id, "candidate_id": candidate_id}
    storage.upload_registry_if_match(path, json.dumps(record, sort_keys=True).encode(), "application/json", etag)
    return record["owner"]


def transition(storage, request_id, candidate_id, owner, before, after):
    path = _path(storage, request_id)
    if path is None:
        raise SerialValidationHeld()
    record, etag = _read(storage, path)
    if (record is None or record["state"] != before or record["owner"] != owner
            or record["request_id"] != request_id or record["candidate_id"] != candidate_id):
        raise SerialValidationHeld()
    record["state"] = after
    storage.upload_registry_if_match(path, json.dumps(record, sort_keys=True).encode(), "application/json", etag)