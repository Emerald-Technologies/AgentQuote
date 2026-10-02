"""Authenticated evidence-job adapter; no model execution during HTTP requests.

Inputs: forwarded identity, bounded JSON, injected service/policy/authorizer.
Outputs: safe job status. Run python -m pytest tests/test_package_evidence_api.py.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Callable

from .package_evidence_contracts import PackageEvidenceError, require
from .package_evidence_jobs import EvidenceJobService, MAX_METADATA


def configured_policy() -> dict:
    from .package_evidence_foundry import POLICY_VERSION, PROMPT_SHA256
    deployment = os.environ.get("PACKAGE_EVIDENCE_MODEL_DEPLOYMENT", "")
    require(bool(deployment.strip()), "MODEL_DEPLOYMENT_REQUIRED")
    return {"producer": "agentquote-foundry", "model": deployment,
            "policyVersion": POLICY_VERSION, "promptSha256": PROMPT_SHA256}


def read_evidence_body(stream, connection, length: int, *, clock=time.monotonic, seconds: float = 10) -> bytes:
    """Total elapsed deadline, not a resettable inactivity timeout."""
    deadline = clock() + seconds
    chunks = []
    remaining = length
    while remaining:
        left = deadline - clock()
        if left <= 0:
            raise TimeoutError
        connection.settimeout(left)
        chunk = stream.read1(min(65536, remaining))
        if not chunk or clock() >= deadline:
            raise TimeoutError
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


class EvidenceApi:
    def __init__(self, *, authorize: Callable, service_factory: Callable[[], EvidenceJobService],
                 policy: dict, enabled: bool = False):
        self.authorize, self.service_factory = authorize, service_factory
        self.policy, self.enabled = dict(policy), enabled

    def handle(self, headers: Any, request_id: str, action: str, body: bytes | None = None) -> tuple[int, dict]:
        try:
            self.authorize(headers)
            if not self.enabled:
                return 503, {"error": {"code": "evidence_disabled", "message": "Evidence generation is disabled."}}
            require(action in {"submit", "start", "status"}, "INVALID_ACTION")
            service = self.service_factory()
            if action == "status":
                return 200, service.status(request_id)
            require(type(body) is bytes and len(body) <= MAX_METADATA, "INVALID_BODY")
            def pairs(items):
                value = {}
                for key, item in items:
                    require(key not in value, "INVALID_JSON")
                    value[key] = item
                return value
            value = json.loads(body.decode("utf-8"), object_pairs_hook=pairs,
                               parse_constant=lambda _: (_ for _ in ()).throw(PackageEvidenceError("INVALID_JSON")))
            if action == "start":
                require(value == {}, "INVALID_BODY")
                request = service.request(request_id)
            else:
                require(type(value) is dict and value.get("requestId") == request_id, "REQUEST_BINDING")
                request = value
            require(request.get("policy") == self.policy, "POLICY_BINDING")
            return (202, service.start(request_id)) if action == "start" else (200, service.submit(request))
        except PermissionError:
            return 401, {"error": {"code": "unauthenticated", "message": "Authentication is required."}}
        except LookupError:
            return 403, {"error": {"code": "forbidden", "message": "Caller is not authorized."}}
        except (PackageEvidenceError, UnicodeError, ValueError, RecursionError):
            return 422, {"error": {"code": "invalid_evidence_request", "message": "Evidence request or artifacts could not be verified."}}
        except Exception:
            return 500, {"error": {"code": "evidence_unavailable", "message": "Evidence operation is unavailable."}}