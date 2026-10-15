"""Run stage 1 (package survey): one model call per batch, every reply checked.

Inputs: package root files as (name, bytes) and a caller that sends one batch to
the agent. Output: the survey record (package_survey_check.assemble). Files whose
records pass are kept; only refused files are asked again, once, with their
stable codes and survey ids. A file still refused is unclassified: a spreadsheet
blocks (package choice retains every BOM), any other kind is a warning. Each
attempt's codes and the refused citations are kept in the finding.
A call that raised without any reply (QQ #182) does not use up one of the two
attempts: the same request is sent again after a short wait, up to
TRANSPORT_RETRIES_PER_BATCH times per batch, and every call counts against the
call ceiling. A reply that is refused still gets exactly one retry.
Run python -m pytest tests/test_package_survey.py.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Callable

from .package_survey import (SPREADSHEET_KINDS, SurveyError, build_view, expand_package,
                             plan_batches, sub_batch, survey_instructions)
from .package_survey_check import assemble, verify_batch_files

SURVEY_RESPONSE_FORMAT = {"type": "json_object"}
ATTEMPTS_PER_BATCH = 2
# QQ #182 (Nick 2026-10-15): a call that raised without a reply is sent again, at most this many
# times per batch, after these waits in seconds; it does not use up a classification attempt.
TRANSPORT_RETRIES_PER_BATCH = 2
TRANSPORT_RETRY_DELAYS = (2.0, 8.0)
MAX_REPLY_BYTES = 1_000_000

Caller = Callable[[str, str], Any]
Sleep = Callable[[float], None]


def survey_package(roots: list[tuple[str, bytes]], call: Caller, *, max_calls: int | None = None,
                   sleep: Sleep | None = None) -> dict[str, Any]:
    """call(instructions, user_text) returns the parsed JSON reply for one batch."""
    return survey_view(build_view(expand_package(roots)), call, max_calls=max_calls, sleep=sleep)


def survey_view(view: Any, call: Caller, *, max_calls: int | None = None,
                sleep: Sleep | None = None) -> dict[str, Any]:
    """The survey of an already built view (stage 2 reuses the same view).

    max_calls caps every model call, transport retries included. By default each batch may use its
    two attempts plus its transport retries, so one batch's retries never take another batch's calls.
    """
    batches = plan_batches(view)
    sleep = sleep or time.sleep
    ceiling = (max_calls if max_calls is not None
               else len(batches) * (ATTEMPTS_PER_BATCH + TRANSPORT_RETRIES_PER_BATCH))
    calls = 0
    records: list[dict] = []
    package_findings: list[dict] = []
    for batch in batches:
        pending = list(batch.file_ids)
        codes: dict[str, str] = {}
        rejected: dict[str, dict[str, Any]] = {}
        attempts: list[dict[str, Any]] = []
        feedback, agent_findings_kept = None, False
        used, transport_retries, wait = 0, 0, 0.0
        while used < ATTEMPTS_PER_BATCH:
            if calls >= ceiling:
                codes.update({file_id: "SURVEY_CALL_LIMIT" for file_id in pending})
                break
            if wait:
                sleep(wait)
            calls += 1
            current = batch if len(pending) == len(batch.file_ids) else sub_batch(view, pending)
            try:
                reply = call(survey_instructions(view), current.user_text + (feedback or ""))
            except SurveyError as exc:
                reply = exc  # A reply came back but could not be read (cut off, not JSON, too long).
            except Exception as exc:
                # No reply came back (QQ #182). The same request is sent again after a wait; only a
                # failure past the transport retries uses up an attempt. The model never saw this
                # request, so the next call carries the same feedback as this one.
                codes.update({file_id: "SURVEY_CALL_FAILED" for file_id in pending})
                attempts.append(_call_failure(exc))
                wait = TRANSPORT_RETRY_DELAYS[min(transport_retries, len(TRANSPORT_RETRY_DELAYS) - 1)]
                if transport_retries < TRANSPORT_RETRIES_PER_BATCH:
                    transport_retries += 1
                else:
                    used += 1
                continue
            used, wait = used + 1, 0.0
            try:
                if isinstance(reply, SurveyError):
                    raise reply
                found, findings, failed = verify_batch_files(view, current, reply)
            except Exception as exc:
                # The reply as a whole was unusable: every pending file is asked again.
                failure = str(exc) if isinstance(exc, SurveyError) else "SURVEY_CALL_FAILED"
                codes.update({file_id: failure for file_id in pending})
                # The exception class name is code, never customer text (round 3, A036 diagnosis).
                attempts.append({"failure_code": failure, "file_id": None,
                                 **({} if isinstance(exc, SurveyError) else {"exception_type": type(exc).__name__})})
                feedback = _retry_text(failure)
                continue
            records.extend(found)
            if not agent_findings_kept:
                # A retry repeats the package view; its package findings would be duplicates.
                package_findings.extend(findings)
                agent_findings_kept = True
            for file_id, error in failed.items():
                codes[file_id] = str(error)
                field = (error.rejected or {}).get("field")
                attempts.append({"failure_code": str(error), "file_id": file_id, **({"field": field} if field else {})})
                if error.rejected is not None:
                    rejected[file_id] = {"file_id": file_id, "failure_code": str(error), **error.rejected}
            pending = [file_id for file_id in batch.file_ids if file_id in failed]
            if not pending:
                break
            feedback = _retry_text([{"file_id": file_id, "failure_code": codes[file_id]} for file_id in pending])
        package_findings.extend(_unclassified(view, records, pending, codes, attempts, rejected))
    return assemble(view, records, package_findings, model_calls=calls)


def _call_failure(exc: Exception) -> dict[str, Any]:
    """The attempt record of a call that raised without a reply: class names and an HTTP status only.

    The exception message may quote the request or the service's reply, so it is never kept (round 3,
    A036 diagnosis). The model client wraps the underlying error, so that error's class and status
    are named too when present.
    """
    cause = exc.__cause__
    status = next((value for value in (getattr(exc, "status_code", None), getattr(cause, "status_code", None))
                   if type(value) is int and 100 <= value <= 599), None)
    return {"failure_code": "SURVEY_CALL_FAILED", "file_id": None, "exception_type": type(exc).__name__,
            **({"cause_type": type(cause).__name__} if cause is not None else {}),
            **({"status_code": status} if status is not None else {})}


def _unclassified(view: Any, records: list[dict], pending: list[str], codes: dict[str, str],
                  attempts: list[dict], rejected: dict[str, dict]) -> list[dict]:
    """Records and findings for files still refused after the retry.

    A refused spreadsheet could be a BOM, so it blocks and package choice retains every BOM.
    A refused PDF, drawing, Gerber or text file is listed with a warning and does not block.
    """
    sheets = [file_id for file_id in pending if view.entries[file_id]["kind"] in SPREADSHEET_KINDS]
    others = [file_id for file_id in pending if file_id not in sheets]
    findings = []
    for group, blocking in ((sheets, True), (others, False)):
        if not group:
            continue
        failure = codes[group[0]]
        if blocking:
            code = "SURVEY_CALL_LIMIT" if failure == "SURVEY_CALL_LIMIT" else "SURVEY_BATCH_FAILED"
            rationale = "These files could not be classified; every BOM is retained for review."
        else:
            code = "SURVEY_FILES_UNCLASSIFIED"
            rationale = "These files are not spreadsheets and could not be classified; they are listed for review and do not block BOM selection."
        finding = {"code": code, "severity": "blocking" if blocking else "warning", "raised_by": "code",
                   "rationale": rationale, "file_ids": list(group), "failure_code": failure,
                   "attempt_failures": attempts,
                   "rejected_citations": [rejected[file_id] for file_id in group if file_id in rejected]}
        findings.append(finding)
        for file_id in group:
            item, entry = view.files[file_id], view.entries[file_id]
            records.append({"file_id": file_id, "path": item.path, "chain": list(item.chain),
                            "sha256": item.sha256, "size_bytes": item.size_bytes, "kind": entry["kind"],
                            "role": "other", "classification_status": "unclassified",
                            "assembly": None, "assembly_evidence": None, "possibly_shared": False,
                            "rationale": rationale, "evidence": [], "findings": [finding],
                            **({"requested_assembly": None} if view.requested else {})})
    return findings


def _retry_text(rejected: str | list[dict[str, str]]) -> str:
    # Only stable check codes and survey ids are returned to the agent, never source values.
    if isinstance(rejected, str):
        feedback = {"previous_reply_rejected": rejected,
                    "instruction": "Return a complete corrected reply for every id in classify."}
    else:
        feedback = {"previous_reply_rejected": rejected,
                    "instruction": "These records were refused; return a complete corrected reply for every id in classify."}
    return json.dumps(feedback, separators=(",", ":"))


def foundry_caller(client: Any = None, agent_factory: Any = None) -> Caller:
    """Same agent and JSON mode as the parser author (foundry_author)."""
    from .foundry_author import _response_text
    from .interpretation_coverage import _pairs_hook

    def call(instructions: str, user_text: str) -> Any:
        async def invoke() -> Any:
            factory = agent_factory
            if factory is None:
                from agent_framework import Agent
                factory = Agent
            try:
                # No output cap (round 7, Nick 2026-10-11): on this reasoning model the cap also counted
                # hidden reasoning and cut off hard batches' JSON (A036, A038, A039).
                response = await factory(client=client, instructions=instructions, tools=[]).run(
                    user_text, options={"response_format": dict(SURVEY_RESPONSE_FORMAT)})
            except ValueError as exc:
                # The model library parses JSON-mode replies itself and raises ValueError on an incomplete
                # reply (A038); named here, and the message (which may quote the reply) is never kept.
                if "not valid JSON" in str(exc):
                    raise SurveyError("SURVEY_REPLY_NOT_JSON") from None
                raise
            # A reply cut off at the output limit is reported as such, not as an unusable call (round 3).
            if str(getattr(response, "finish_reason", "") or "").lower().endswith("length"):
                raise SurveyError("SURVEY_REPLY_TRUNCATED")
            text = _response_text(response)
            if len(text.encode("utf-8")) > MAX_REPLY_BYTES:
                raise SurveyError("SURVEY_REPLY_LIMIT")
            try:
                return json.loads(text, object_pairs_hook=_pairs_hook)
            except ValueError:
                raise SurveyError("SURVEY_REPLY_NOT_JSON") from None
        return asyncio.run(invoke())
    return call
