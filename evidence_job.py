"""Evidence worker: REQUEST_ID only; actual Government model/policy server-owned.

Loads immutable request and pinned bytes, publishes a proposal, never approval.
Run python evidence_job.py in an explicitly enabled configured Job; test offline.
Logs one agentquote-job-line.v1 START and END line (bom_processor/job_identity_log.py) before the
existing final result line.
"""
from __future__ import annotations

import json
import os
import re

from bom_processor.job_identity_log import JobLog
from bom_processor.package_evidence_contracts import failure_causes, require  # noqa: F401 (re-exported)
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
    log = JobLog("evidence")
    try:
        require(os.environ.get("PACKAGE_EVIDENCE_MODEL_ENABLED") == "true", "MODEL_DISABLED")
        require(not any(key.startswith("REQUEST_") and key != "REQUEST_ID" for key in os.environ), "INVALID_ENVIRONMENT")
        from deterministic_api import _storage_factory
        from bom_processor import job_identity_log as job_log
        from bom_processor.package_evidence_foundry import create_foundry_evidence_model
        storage = _storage_factory()
        request_id = os.environ.get("REQUEST_ID", "")
        log.start(lambda: ("evidence", job_log.load_evidence_identity(storage, request_id)))
        result = run_worker(EvidenceJobService(storage), request_id,
                            configured_policy(), create_foundry_evidence_model)
        log.end(*job_log.state_outcome(result, "EVIDENCE_JOB_FAILED"))
        print(json.dumps({"requestId": result["requestId"], "status": result["status"]}, sort_keys=True))
        return 0
    except Exception as exc:
        causes = failure_causes(exc)
        # The outermost stable code is the most specific one; class names are not codes.
        log.end("failed", next((cause for cause in causes if re.fullmatch(r"[A-Z][A-Z0-9_]+", cause)),
                               "EVIDENCE_WORKER_FAILED"))
        print(json.dumps({"status": "error", "code": "EVIDENCE_WORKER_FAILED", "causes": causes}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())