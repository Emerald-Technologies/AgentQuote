"""Package choice worker (plan 8.8 step 2): REQUEST_ID only; model and storage are server-owned.

Inputs: REQUEST_ID (a package-choice request written by the API), managed identity, storage URL
and Foundry endpoint from the job environment. Output: package-choice/<REQUEST_ID>/ result and
completed (or failed) markers, and one log line with any survey failure codes, preceded by one
agentquote-job-line.v1 START and END line (bom_processor/job_identity_log.py).
Never approves anything; the BOM request reads the result.
Run python choice_job.py inside the configured Container Apps Job; tests run offline.
"""
from __future__ import annotations

import json
import os

from bom_processor.job_identity_log import JobLog


def _summary(state: dict) -> dict:
    """Status plus stable survey failure codes, survey file ids and counts; never package values."""
    line = {"request_id": state["request_id"], "status": state["status"]}
    if state["status"] == "failed":
        line["code"] = state.get("code")
    survey = (state.get("result") or {}).get("survey") or state.get("survey") or {}
    # Refused citation values stay in the stored result; the log line carries codes and ids only.
    failures = [{"code": item["code"], "severity": item.get("severity"), "failure_code": item.get("failure_code"),
                 "file_ids": item.get("file_ids", []), "attempts": item.get("attempt_failures", [])}
                for item in survey.get("findings", [])
                if item.get("code") in ("SURVEY_BATCH_FAILED", "SURVEY_CALL_LIMIT", "SURVEY_FILES_UNCLASSIFIED")]
    if failures:
        line["survey_failures"] = failures
    return line


def _failure_code(exc: Exception) -> str:
    """The stable PackageChoiceError code when there is one (never a message), else CHOICE_WORKER_FAILED."""
    try:
        from bom_processor.bom_package_choice import PackageChoiceError, _stable_code
        if isinstance(exc, PackageChoiceError) and _stable_code(exc) != "PACKAGE_CHOICE_FAILED":
            return _stable_code(exc)
    except Exception:
        pass
    return "CHOICE_WORKER_FAILED"


def main() -> int:
    log = JobLog("choice")
    request_id = os.environ.get("REQUEST_ID", "")
    if not request_id or any(key.startswith("REQUEST_") and key != "REQUEST_ID" for key in os.environ):
        log.end("failed", "INVALID_ENVIRONMENT")
        print(json.dumps({"status": "error", "code": "INVALID_ENVIRONMENT"}, sort_keys=True))
        return 2
    log.note(request_id=request_id)
    try:
        from deterministic_api import _storage_factory
        from bom_processor import job_identity_log as job_log
        from bom_processor.bom_package_choice import PackageChoiceService, create_choice_caller
        storage = _storage_factory()
        log.start(lambda: ("choice", job_log.load_choice_identity(storage, request_id)))
        result = PackageChoiceService(storage).run(request_id, create_choice_caller())
        log.end(*job_log.state_outcome(result, result.get("code")))
        print(json.dumps(_summary(result), sort_keys=True))
        return 0
    except Exception as exc:
        # Class name only: exception messages can hold package data.
        log.end("failed", _failure_code(exc))
        print(json.dumps({"status": "error", "code": "CHOICE_WORKER_FAILED",
                          "cause": type(exc).__name__}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
