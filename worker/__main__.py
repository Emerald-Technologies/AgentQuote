"""CLI entry point for the parser worker."""
import json
import sys

from .runtime import run_job
from .transfer import REQUIRED, remote_job
import os


def main() -> int:
    worker_keys = {key for key in os.environ if key.startswith("WORKER_")}
    configured = [key for key in REQUIRED if os.environ.get(key)]
    expected_worker_keys = {
        "WORKER_JOB_ID", "WORKER_INPUT_NAME", "WORKER_OUTPUT_NAME",
        "WORKER_OUTPUT_CONTENT_TYPE",
    }
    if configured and (worker_keys != expected_worker_keys or len(configured) != len(REQUIRED)):
        missing = sorted(key for key in REQUIRED if not os.environ.get(key))
        unknown = sorted(worker_keys - expected_worker_keys)
        result, code = {
            "contract_version": "remote-worker.v1",
            "job": os.environ.get("WORKER_JOB_ID", "invalid"),
            "status": "failed",
            "failure": "invalid_configuration",
            "missing_names": missing,
            "unknown_names": unknown,
        }, 2
    elif configured:
        result, code = remote_job()
    else:
        result, code = run_job()
    sys.stdout.write(json.dumps(result, separators=(",", ":"), sort_keys=True) + "\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
