"""Evidence worker: REQUEST_ID only; actual Government model/policy server-owned.

Loads immutable request and pinned bytes, publishes a proposal, never approval.
Run python evidence_job.py in an explicitly enabled configured Job; test offline.
"""
from __future__ import annotations

import json
import os

from bom_processor.package_evidence_contracts import require
from bom_processor.package_evidence_api import configured_policy
from bom_processor.package_evidence_jobs import EvidenceJobService


def run_worker(service: EvidenceJobService, request_id: str, policy: dict, model_factory) -> dict:
    request = service.request(request_id)
    require(request["policy"] == policy, "POLICY_BINDING")
    state = service.status(request_id)
    if state["status"] in {"completed", "failed", "running"}:
        return state
    model = model_factory()
    require(model.model_name == policy["model"], "MODEL_BINDING")
    return service.run(request_id, model)


def main() -> int:
    try:
        require(os.environ.get("PACKAGE_EVIDENCE_MODEL_ENABLED") == "true", "MODEL_DISABLED")
        require(not any(key.startswith("REQUEST_") and key != "REQUEST_ID" for key in os.environ), "INVALID_ENVIRONMENT")
        from deterministic_api import _storage_factory
        from bom_processor.package_evidence_foundry import create_foundry_evidence_model
        result = run_worker(EvidenceJobService(_storage_factory()), os.environ.get("REQUEST_ID", ""),
                            configured_policy(), create_foundry_evidence_model)
        print(json.dumps({"requestId": result["requestId"], "status": result["status"]}, sort_keys=True))
        return 0
    except Exception:
        print(json.dumps({"status": "error", "code": "EVIDENCE_WORKER_FAILED"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())