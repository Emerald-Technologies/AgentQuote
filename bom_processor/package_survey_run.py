"""Run stage 1 (package survey): one model call per batch, every reply checked.

Inputs: package root files as (name, bytes) and a caller that sends one batch to
the agent. Output: the survey record (package_survey_check.assemble). A reply
that fails a check is retried once with the stable failure code and the failing
survey file id; each attempt's code and file id are kept in the finding; a second
failure leaves its files unclassified with a blocking finding. Other batches
continue; package choice retains every BOM when a batch failed (plan 8.8 step 6). Run python -m pytest tests/test_package_survey.py.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Callable

from .package_survey import (SURVEY_INSTRUCTIONS, SurveyBatch, SurveyError, build_view,
                             expand_package, plan_batches)
from .package_survey_check import verify_batch, assemble

SURVEY_MAX_TOKENS = 8192
SURVEY_RESPONSE_FORMAT = {"type": "json_object"}
ATTEMPTS_PER_BATCH = 2
MAX_REPLY_BYTES = 1_000_000

Caller = Callable[[str, str], Any]


def survey_package(roots: list[tuple[str, bytes]], call: Caller, *, max_calls: int | None = None) -> dict[str, Any]:
    """call(instructions, user_text) returns the parsed JSON reply for one batch."""
    return survey_view(build_view(expand_package(roots)), call, max_calls=max_calls)


def survey_view(view: Any, call: Caller, *, max_calls: int | None = None) -> dict[str, Any]:
    """The survey of an already built view (stage 2 reuses the same view)."""
    batches = plan_batches(view)
    ceiling = max_calls if max_calls is not None else len(batches) * ATTEMPTS_PER_BATCH
    calls = 0
    records: list[dict] = []
    package_findings: list[dict] = []
    for batch in batches:
        failure, failed_file = None, None
        attempts: list[dict[str, Any]] = []
        for _ in range(ATTEMPTS_PER_BATCH):
            if calls >= ceiling:
                failure, failed_file = "SURVEY_CALL_LIMIT", None
                break
            calls += 1
            text = batch.user_text if failure is None else _retry_text(batch, failure, failed_file)
            try:
                found, findings = verify_batch(view, batch, call(SURVEY_INSTRUCTIONS, text))
            except Exception as exc:
                failure = str(exc) if isinstance(exc, SurveyError) else "SURVEY_CALL_FAILED"
                failed_file = exc.file_id if isinstance(exc, SurveyError) else None
                attempts.append({"failure_code": failure, "file_id": failed_file})
                continue
            records.extend(found)
            package_findings.extend(findings)
            failure = None
            break
        if failure is not None:
            code = "SURVEY_CALL_LIMIT" if failure == "SURVEY_CALL_LIMIT" else "SURVEY_BATCH_FAILED"
            finding = {"code": code, "severity": "blocking", "raised_by": "code",
                       "rationale": "These files could not be classified; every BOM is retained for review.",
                       "file_ids": list(batch.file_ids), "failure_code": failure,
                       "attempt_failures": attempts}
            package_findings.append(finding)
            for file_id in batch.file_ids:
                item, entry = view.files[file_id], view.entries[file_id]
                records.append({"file_id": file_id, "path": item.path, "chain": list(item.chain),
                                "sha256": item.sha256, "size_bytes": item.size_bytes, "kind": entry["kind"],
                                "role": "other", "classification_status": "unclassified",
                                "assembly": None, "assembly_evidence": None, "possibly_shared": False,
                                "rationale": finding["rationale"], "evidence": [], "findings": [finding]})
    return assemble(view, records, package_findings, model_calls=calls)


def _retry_text(batch: SurveyBatch, failure: str, file_id: str | None = None) -> str:
    # Only the stable check code and survey id are returned to the agent, never source values.
    feedback: dict[str, Any] = {"previous_reply_rejected": failure}
    if file_id is not None:
        feedback["file_id"] = file_id
    feedback["instruction"] = ("Return a complete corrected reply for every id in classify"
                               + ("; correct the record for file_id." if file_id is not None else "."))
    return batch.user_text + json.dumps(feedback, separators=(",", ":"))


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
            response = await factory(client=client, instructions=instructions, tools=[]).run(
                user_text, options={"max_tokens": SURVEY_MAX_TOKENS,
                                    "response_format": dict(SURVEY_RESPONSE_FORMAT)})
            text = _response_text(response)
            if len(text.encode("utf-8")) > MAX_REPLY_BYTES:
                raise SurveyError("SURVEY_REPLY_LIMIT")
            try:
                return json.loads(text, object_pairs_hook=_pairs_hook)
            except ValueError:
                raise SurveyError("SURVEY_REPLY_NOT_JSON") from None
        return asyncio.run(invoke())
    return call
