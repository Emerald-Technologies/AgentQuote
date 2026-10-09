"""Package choice worker (plan 8.8 step 2): REQUEST_ID only; model and storage are server-owned.

Inputs: REQUEST_ID (a package-choice request written by the API), managed identity, storage URL
and Foundry endpoint from the job environment. Output: package-choice/<REQUEST_ID>/ result and
completed (or failed) markers. Never approves anything; the BOM request reads the result.
Run python choice_job.py inside the configured Container Apps Job; tests run offline.
"""
from __future__ import annotations

import json
import os


def main() -> int:
    request_id = os.environ.get("REQUEST_ID", "")
    if not request_id or any(key.startswith("REQUEST_") and key != "REQUEST_ID" for key in os.environ):
        print(json.dumps({"status": "error", "code": "INVALID_ENVIRONMENT"}, sort_keys=True))
        return 2
    try:
        from deterministic_api import _storage_factory
        from bom_processor.bom_package_choice import PackageChoiceService, create_choice_caller
        result = PackageChoiceService(_storage_factory()).run(request_id, create_choice_caller())
        print(json.dumps({"request_id": result["request_id"], "status": result["status"]}, sort_keys=True))
        return 0
    except Exception as exc:
        # Class name only: exception messages can hold package data.
        print(json.dumps({"status": "error", "code": "CHOICE_WORKER_FAILED",
                          "cause": type(exc).__name__}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
