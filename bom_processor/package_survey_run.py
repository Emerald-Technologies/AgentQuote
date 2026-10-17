"""Run stage 1 (package survey): one model call per batch, every reply checked.

Inputs: package root files as (name, bytes) and a caller that sends one batch to
the agent. Output: the survey record (package_survey_check.assemble). Files whose
records pass are kept; only refused files are asked again, once, with their
stable codes and survey ids. Each attempt's codes and the refused citations are
kept in the finding.
Without requested assemblies (v1), a file still refused is unclassified: a
spreadsheet blocks (package choice retains every BOM), any other kind is a warning.
With requested assemblies (QQ #99, Nick 2026-10-14), files are surveyed most
likely BOM first and less likely files only until every requested assembly has a
confirmed BOM; the rest are listed as not surveyed. A file still refused is
listed as not examined with a warning, and blocks only when the evidence says
the BOM choice is impossible or unsafe without it (package_survey_rank.py).
An empty or header-only spreadsheet is never sent to the model: it is listed as
'other' with a "nothing to read" note and never fails the package (QQ #99 D5).
A call that raised without any reply (QQ #182) does not use up one of the two
attempts: the same request is sent again after a short wait, up to
TRANSPORT_RETRIES_PER_BATCH times per batch, and every call counts against the
call ceiling. A reply that is refused still gets exactly one retry.
Run python -m pytest tests/test_package_survey.py tests/test_package_survey_rank.py.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .document_role import ROLES
from .package_survey import (SPREADSHEET_KINDS, SurveyError, build_view, expand_package, nothing_to_read,
                             plan_batches, sub_batch, survey_instructions)
from .package_survey_check import assemble, verify_batch_files

SURVEY_RESPONSE_FORMAT = {"type": "json_object"}
ATTEMPTS_PER_BATCH = 2
# QQ #182 (Nick 2026-10-15): a call that raised without a reply is sent again, at most this many
# times per batch, after these waits in seconds; it does not use up a classification attempt.
TRANSPORT_RETRIES_PER_BATCH = 2
TRANSPORT_RETRY_DELAYS = (2.0, 8.0)
CALLS_PER_BATCH = ATTEMPTS_PER_BATCH + TRANSPORT_RETRIES_PER_BATCH
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

    With requested assemblies the files are surveyed most likely BOM first and a refused file is
    judged by package_survey_rank (QQ #99); without them every file is surveyed in path order.
    max_calls caps every model call, transport retries included. By default each batch may use its
    two attempts plus its transport retries, so one batch's retries never take another batch's calls.
    """
    if view.requested:
        return _ranked_survey(view, call, max_calls=max_calls, sleep=sleep)
    batches = plan_batches(view)
    budget = _Budget(max_calls if max_calls is not None else len(batches) * CALLS_PER_BATCH, sleep or time.sleep)
    records: list[dict] = []
    package_findings: list[dict] = []
    for batch in batches:
        outcome = _survey_batch(view, batch, call, budget, records, package_findings)
        package_findings.extend(_unclassified(view, records, outcome.pending, outcome.codes, outcome.attempts,
                                              outcome.rejected))
    return assemble(view, records, package_findings, model_calls=budget.used)


@dataclass
class _Budget:
    ceiling: int
    sleep: Sleep
    used: int = 0


@dataclass
class _Outcome:
    """Files of one batch still refused after the retry, with each attempt's codes."""
    pending: list[str]
    codes: dict[str, str]
    attempts: list[dict[str, Any]]
    rejected: dict[str, dict[str, Any]]
    proposed: dict[str, str] = field(default_factory=dict)


def _survey_batch(view: Any, batch: Any, call: Caller, budget: _Budget, records: list[dict],
                  package_findings: list[dict]) -> _Outcome:
    """Survey one batch: records that pass are kept; only refused files are asked again, once."""
    pending = list(batch.file_ids)
    outcome = _Outcome(pending, {}, [], {})
    feedback, agent_findings_kept = None, False
    used, transport_retries, wait = 0, 0, 0.0
    while used < ATTEMPTS_PER_BATCH:
        if budget.used >= budget.ceiling:
            outcome.codes.update({file_id: "SURVEY_CALL_LIMIT" for file_id in pending})
            break
        if wait:
            budget.sleep(wait)
        budget.used += 1
        current = batch if len(pending) == len(batch.file_ids) else sub_batch(view, pending)
        try:
            reply = call(survey_instructions(view), current.user_text + (feedback or ""))
        except SurveyError as exc:
            reply = exc  # A reply came back but could not be read (cut off, not JSON, too long).
        except Exception as exc:
            # No reply came back (QQ #182). The same request is sent again after a wait; only a
            # failure past the transport retries uses up an attempt. The model never saw this
            # request, so the next call carries the same feedback as this one.
            outcome.codes.update({file_id: "SURVEY_CALL_FAILED" for file_id in pending})
            outcome.attempts.append(_call_failure(exc))
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
            outcome.codes.update({file_id: failure for file_id in pending})
            # The exception class name is code, never customer text (round 3, A036 diagnosis).
            outcome.attempts.append({"failure_code": failure, "file_id": None,
                                     **({} if isinstance(exc, SurveyError) else {"exception_type": type(exc).__name__})})
            feedback = _retry_text(failure)
            continue
        records.extend(found)
        if not agent_findings_kept:
            # A retry repeats the package view; its package findings would be duplicates.
            package_findings.extend(findings)
            agent_findings_kept = True
        for file_id, error in failed.items():
            outcome.codes[file_id] = str(error)
            field_name = (error.rejected or {}).get("field")
            outcome.attempts.append({"failure_code": str(error), "file_id": file_id,
                                     **({"field": field_name} if field_name else {})})
            if error.rejected is not None:
                outcome.rejected[file_id] = {"file_id": file_id, "failure_code": str(error), **error.rejected}
        outcome.proposed.update(_proposed_roles(reply, failed))
        pending = [file_id for file_id in batch.file_ids if file_id in failed]
        if not pending:
            break
        feedback = _retry_text([{"file_id": file_id, "failure_code": outcome.codes[file_id]} for file_id in pending])
    outcome.pending = pending
    return outcome


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


def _proposed_roles(reply: Any, failed: dict[str, Any]) -> dict[str, str]:
    """The role the model gave each refused file: unverified, kept only as a clue (QQ #99)."""
    roles = {}
    for proposal in reply.get("files", []) if isinstance(reply, dict) else []:
        if isinstance(proposal, dict) and proposal.get("file_id") in failed and proposal.get("role") in ROLES:
            roles[proposal["file_id"]] = proposal["role"]
    return roles


def _ranked_survey(view: Any, call: Caller, *, max_calls: int | None, sleep: Sleep | None) -> dict[str, Any]:
    """Survey likely BOM candidates first; stop once each requested assembly has a confirmed BOM (QQ #99).

    Every likely candidate is surveyed, so a competing BOM for an assembly is never skipped. Less
    likely files follow, in rank order, only while some requested assembly lacks a confirmed BOM;
    the rest are listed as not surveyed with no model call. A file whose reading is still refused
    after its retry is listed as not examined with a warning, unless the evidence says it could be
    a chosen BOM or the choice is impossible without it (package_survey_rank.refusal_blocks).
    An empty or header-only spreadsheet is never sent to the model (QQ #99 D5): it has no parts to
    read, so it is listed as 'other' with a "nothing to read" note and the package goes ahead.
    """
    from .package_survey_rank import RANK_VERSION, every_assembly_confirmed, rank_files, refusal_blocks
    ranking = rank_files(view)
    empty = {item["file_id"] for item in ranking if nothing_to_read(view, item["file_id"])}
    phases = [plan_batches(view, file_ids=[item["file_id"] for item in ranking
                                           if item["likely"] and item["file_id"] not in empty]),
              plan_batches(view, file_ids=[item["file_id"] for item in ranking
                                           if not item["likely"] and item["file_id"] not in empty])]
    budget = _Budget(max_calls if max_calls is not None
                     else sum(len(batches) for batches in phases) * CALLS_PER_BATCH, sleep or time.sleep)
    records: list[dict] = []
    package_findings: list[dict] = []
    refused: list[str] = []
    codes: dict[str, str] = {}
    attempts: list[dict[str, Any]] = []
    rejected: dict[str, dict[str, Any]] = {}
    proposed: dict[str, str] = {}
    skipped: list[str] = []
    if empty:
        package_findings.append(_nothing_to_read(view, records, empty))
    for likely, batches in zip((True, False), phases):
        for batch in batches:
            if not likely and every_assembly_confirmed(view, records):
                skipped.extend(batch.file_ids)
                continue
            outcome = _survey_batch(view, batch, call, budget, records, package_findings)
            if not outcome.pending:
                continue
            refused.extend(outcome.pending)
            codes.update(outcome.codes)
            attempts.extend(item for item in outcome.attempts
                            if item["file_id"] is None or item["file_id"] in outcome.pending)
            rejected.update({key: value for key, value in outcome.rejected.items() if key in outcome.pending})
            proposed.update({key: value for key, value in outcome.proposed.items() if key in outcome.pending})
    blocks = refusal_blocks(view, ranking, records, refused, proposed)
    package_findings.extend(_refused_findings(view, records, blocks, codes, attempts, rejected))
    if skipped:
        package_findings.append(_not_surveyed(view, records, skipped))
    survey = assemble(view, records, package_findings, model_calls=budget.used)
    reasons = {file_id: "not_needed" for file_id in skipped}
    reasons.update({file_id: "call_limit" if codes[file_id] == "SURVEY_CALL_LIMIT" else "evidence_refused"
                    for file_id in refused})
    # A file with nothing to read was examined (by code): its reason says why it holds no parts.
    survey["examination"] = {
        "version": RANK_VERSION, "stopped_early": bool(skipped),
        "files": [{**item, "status": "not_examined" if item["file_id"] in reasons else "examined",
                   "reason": NOTHING_TO_READ if item["file_id"] in empty else reasons.get(item["file_id"]),
                   **({"blocks": blocks[item["file_id"]]} if item["file_id"] in blocks else {})}
                  for item in ranking]}
    return survey


NOT_EXAMINED = "not_examined"
NOT_SURVEYED = "not_surveyed"
NOTHING_TO_READ = "nothing_to_read"


def _base_record(view: Any, file_id: str) -> dict[str, Any]:
    item, entry = view.files[file_id], view.entries[file_id]
    return {"file_id": file_id, "path": item.path, "chain": list(item.chain), "sha256": item.sha256,
            "size_bytes": item.size_bytes, "kind": entry["kind"], "assembly": None, "assembly_evidence": None,
            "possibly_shared": False, "evidence": [], "requested_assembly": None, "refused_citations": []}


def _refused_findings(view: Any, records: list[dict], blocks: dict[str, str | None], codes: dict[str, str],
                      attempts: list[dict], rejected: dict[str, dict]) -> list[dict]:
    """Findings and records for files still refused after the retry, judged by refusal_blocks.

    A blocking file stops the package (PACKAGE_SURVEY_INCOMPLETE) as before; any other is listed as
    not examined with a warning that names it, and BOM choice goes ahead without it.
    """
    blocked = [file_id for file_id, reason in blocks.items() if reason is not None]
    groups = [("SURVEY_CALL_LIMIT", [file_id for file_id in blocked if codes[file_id] == "SURVEY_CALL_LIMIT"]),
              ("SURVEY_BATCH_FAILED", [file_id for file_id in blocked if codes[file_id] != "SURVEY_CALL_LIMIT"]),
              ("SURVEY_FILES_UNCLASSIFIED", [file_id for file_id, reason in blocks.items() if reason is None])]
    findings = []
    for code, group in groups:
        if not group:
            continue
        group = sorted(group, key=lambda value: int(value[1:]))
        blocking = code != "SURVEY_FILES_UNCLASSIFIED"
        rationale = ("These files could not be classified, and one of them could be a requested assembly's BOM "
                     "or the BOM choice is impossible without them; every BOM is retained for review."
                     if blocking else
                     "The survey could not verify a reading of these files after its retry. They are listed as not "
                     "examined; none could be a chosen BOM for a requested assembly, so BOM choice went ahead.")
        finding = {"code": code, "severity": "blocking" if blocking else "warning", "raised_by": "code",
                   "rationale": rationale, "file_ids": group, "failure_code": codes[group[0]],
                   "attempt_failures": [item for item in attempts if item["file_id"] is None or item["file_id"] in group],
                   "rejected_citations": [rejected[file_id] for file_id in group if file_id in rejected]}
        if blocking:
            finding["block_reasons"] = {file_id: blocks[file_id] for file_id in group}
        findings.append(finding)
        for file_id in group:
            if blocking:
                records.append({**_base_record(view, file_id), "role": "other", "classification_status": "unclassified",
                                "rationale": rationale, "findings": [finding]})
            else:
                records.append({**_base_record(view, file_id), "role": NOT_EXAMINED,
                                "classification_status": NOT_EXAMINED,
                                "not_examined_reason": "call_limit" if codes[file_id] == "SURVEY_CALL_LIMIT"
                                else "evidence_refused",
                                "rationale": rationale, "findings": [finding]})
    return findings


def _nothing_to_read(view: Any, records: list[dict], empty: set[str]) -> dict[str, Any]:
    """Records and the note for empty or header-only spreadsheets: 'other', never a failure (D5)."""
    rationale = ("Nothing to read: this spreadsheet is empty or holds only a heading row, so it has no parts "
                 "and is not a BOM candidate. It was not sent to the survey model.")
    note = {"code": "SURVEY_NOTHING_TO_READ", "severity": "information", "raised_by": "code", "rationale": rationale}
    file_ids = sorted(empty, key=lambda value: int(value[1:]))
    for file_id in file_ids:
        records.append({**_base_record(view, file_id), "role": "other", "classification_status": "classified",
                        "rationale": rationale, "findings": [dict(note)]})
    return {**note, "file_ids": file_ids}


def _not_surveyed(view: Any, records: list[dict], skipped: list[str]) -> dict[str, Any]:
    """Records and the finding for less likely files left unsurveyed once every assembly was confirmed."""
    rationale = ("Not needed for BOM choice: every requested assembly already has a confirmed BOM among the "
                 "more likely files, so this less likely file was listed but not surveyed.")
    for file_id in skipped:
        records.append({**_base_record(view, file_id), "role": NOT_SURVEYED, "classification_status": NOT_SURVEYED,
                        "not_examined_reason": "not_needed", "rationale": rationale, "findings": []})
    return {"code": "SURVEY_FILES_NOT_SURVEYED", "severity": "information", "raised_by": "code",
            "rationale": "Every requested assembly has a confirmed BOM among the more likely files; these less "
                         "likely files are listed and were not surveyed.",
            "file_ids": sorted(skipped, key=lambda value: int(value[1:]))}


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
