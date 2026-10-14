"""Bounded, fail-closed status projection for one candidate request."""
from __future__ import annotations

import json
import re
from typing import Any

from .core import ProcessingError
from .candidate_review import CandidateReviewError, CandidateReviewService

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_HASH = re.compile(r"^[0-9a-fA-F]{64}$")
_MAX = 100


def _authoring_rank(value: Any) -> tuple[int, str]:
    if not isinstance(value, str):
        return (0, "")
    match = re.fullmatch(r"candidate-authoring\.v(\d+)", value)
    return (int(match.group(1)), value) if match else (0, value)


class CandidateStatusError(ProcessingError):
    pass


def _manufacturer_retry_hint(event: Any) -> dict[str, Any] | None:
    """A047: the lookup hint of a manufacturer stop that may get one automatic retry."""
    from .manufacturer_lookup import retry_hint
    return retry_hint(event) if isinstance(event, dict) else None


def _json(storage: Any, path: str, candidate: bool) -> dict[str, Any]:
    try:
        data = (storage.download_candidate if candidate else storage.download_registry)(path, 1_000_000)
        value = json.loads(data.decode("utf-8"))
    except Exception as exc:
        raise CandidateStatusError("invalid candidate artifact") from exc
    if not isinstance(value, dict):
        raise CandidateStatusError("invalid candidate artifact")
    return value


class CandidateStatusService:
    def __init__(self, storage: Any, *, max_results: int = _MAX, automation: Any = None) -> None:
        if type(max_results) is not int or not 1 <= max_results <= _MAX:
            raise ValueError("invalid candidate status limit")
        self.storage, self.max_results, self.automation = storage, max_results, automation

    def get(self, request_id: str) -> dict[str, Any]:
        if not isinstance(request_id, str) or not _ID.fullmatch(request_id):
            raise CandidateStatusError("invalid request id")
        request_path = f"candidates/requests/{request_id}.json"
        request = _json(self.storage, request_path, False)
        if (request.get("request_id") != request_id or not isinstance(request.get("parser_key"), str)
                or not isinstance(request.get("customer_key"), str)
                or not _HASH.fullmatch(str(request.get("profile_hash", "")))):
            raise CandidateStatusError("invalid request artifact")
        source = request.get("source_evidence")
        if not isinstance(source, list) or any(not isinstance(item, dict) for item in source):
            raise CandidateStatusError("invalid request artifact")
        root = f"candidates/{request_id}/"
        paths = self.storage.list_candidate_paths(root, self.max_results)
        decision_root = f"candidates/decisions/{request_id}/"
        decision_paths = self.storage.list_candidate_paths(decision_root, self.max_results)
        paths.extend(decision_paths)
        if len(paths) > self.max_results or any(
            not (path.startswith(root) or path.startswith(decision_root))
            or ".." in path.split("/") for path in paths):
            raise CandidateStatusError("candidate namespace violation")
        manifests: dict[str, dict[str, Any]] = {}
        evidence: dict[str, list[dict[str, Any]]] = {}
        decisions: list[dict[str, Any]] = []
        for path in paths:
            if path.startswith(f"candidates/decisions/{request_id}/") and path.endswith(".json"):
                decisions.append(_json(self.storage, path, False))
            elif path.startswith(root) and path.endswith("/candidate-manifest.json"):
                item = _json(self.storage, path, True)
                candidate_id = path[len(root):-len("/candidate-manifest.json")]
                if not _ID.fullmatch(candidate_id) or item.get("candidate_id") != candidate_id or item.get("request_id") != request_id:
                    raise CandidateStatusError("candidate namespace violation")
                manifests[candidate_id] = item
            elif path.startswith(root) and "/validation/" in path and path.endswith("/evidence.json"):
                item = _json(self.storage, path, True)
                candidate_id = path[len(root):].split("/", 1)[0]
                if not _ID.fullmatch(candidate_id) or item.get("candidate_id") != candidate_id or item.get("request_id") != request_id:
                    raise CandidateStatusError("candidate namespace violation")
                evidence.setdefault(candidate_id, []).append(item)
        records = []
        budget_candidate = None
        budget_state = None
        from .core import BlobNotFoundError
        def has_budget_record(path: str) -> bool:
            try:
                self.storage.download_registry(path, 100_000)
                return True
            except (BlobNotFoundError, FileNotFoundError):
                return False
        budget_paths = has_budget_record(f"author-budget/v1/members/{request_id}.json")
        required_paths = has_budget_record(f"author-budget/v1/required/{request_id}.json")
        if (budget_paths or required_paths
            or self.automation and getattr(self.automation, "author_budget_enabled", False)):
            from .author_budget import Budget
            options = {"clock": self.automation.budget_clock} if self.automation and getattr(self.automation, "author_budget_enabled", False) else {}
            budget = Budget(self.storage, request_id, **options)
            budget_state = budget.inspect()
            if budget_state["status"] != "legacy":
                try:
                    budget_candidate = budget.selected_candidate()
                except Exception:
                    budget_state = {"status": "hold", "reason": "budget_invalid"}
        for candidate_id, manifest in manifests.items():
            if manifest.get("status") not in {"generated_unvalidated"}:
                raise CandidateStatusError("invalid candidate artifact")
            ev = evidence.get(candidate_id, [])
            from .validation_policy import superseded_layout_failure
            if sum(superseded_layout_failure(item) for item in ev) > 1:
                raise CandidateStatusError("ambiguous validation evidence")
            for item in ev:
                if superseded_layout_failure(item):
                    if (item["request_id"] != request_id or item["candidate_id"] != candidate_id
                            or item["candidate"]["sha256"] != manifest.get("code_sha256")
                            or len(source) != 1 or item["fixtures"][0].get("sha256") != source[0].get("sha256")):
                        raise CandidateStatusError("invalid validation evidence binding")
            ev = [item for item in ev if not superseded_layout_failure(item)]
            if len(ev) > 1:
                raise CandidateStatusError("ambiguous validation evidence")
            current = "generated_unvalidated"
            validation_id = None
            if ev:
                validation_id = ev[0].get("validation_id")
                fixtures = ev[0].get("fixtures")
                if (ev[0].get("version") in {"candidate-validation.v3", "candidate-validation.v4"}
                        and isinstance(fixtures, list) and fixtures
                        and any(isinstance(item, dict) and "fixture_sha256" in item for item in fixtures)):
                    complete = (ev[0].get("complete") is True
                                and ev[0].get("fixture_count") == len(fixtures))
                    passed = complete and all(
                        isinstance(item, dict) and item.get("status") == "passed"
                        for item in fixtures)
                    current = "awaiting_approval" if passed and ev[0].get("status") == "awaiting_approval" else "schema_invalid"
                else:
                    current = ("awaiting_approval"
                               if ev[0].get("schema", {}).get("valid") is True
                               and ev[0].get("semantic", {}).get("valid") is True
                               and ev[0].get("arena_policy", {}).get("valid", True) is True
                               else "schema_invalid")
            matching = [d for d in decisions if d.get("request_id") == request_id and d.get("candidate_id") == candidate_id]
            if len(matching) > 1:
                raise CandidateStatusError("ambiguous decisions")
            decision = matching[0] if matching else None
            if decision:
                current = "approved" if decision.get("decision") == "approve" else "rejected"
                validation_id = decision.get("validation_id", validation_id)
            priority = {"generated_unvalidated": 1, "schema_invalid": 1, "awaiting_approval": 2,
                        "approved": 3, "rejected": 3}[current]
            records.append((_authoring_rank(manifest.get("version")), priority,
                            candidate_id, current, validation_id, manifest, decision))
        if not records:
            status, selected = request.get("status", "requested"), None
        else:
            top = max(row[0] for row in records)
            choices = [row for row in records if row[0] == top]
            if budget_candidate is not None:
                choices = [row for row in records if row[2] == budget_candidate]
                if len(choices) != 1:
                    raise CandidateStatusError("invalid budget candidate lineage")
            if len(choices) != 1:
                # A refinement re-authors a candidate at the same authoring
                # version, so the superseded (refined) candidate must not tie
                # with its successor. Drop any candidate that a started
                # author-refinement marker names as its refinement source.
                superseded = self._superseded_by_refinement(request_id)
                remaining = [row for row in choices if row[2] not in superseded]
                if len(remaining) != 1:
                    raise CandidateStatusError("ambiguous candidates")
                choices = remaining
            _, _, candidate_id, status, validation_id, manifest, decision = choices[0]
            selected = (candidate_id, status, validation_id, manifest, decision)
        result = {"request_id": request_id, "parser_key": request["parser_key"], "customer_key": request["customer_key"],
                  "status": status, "profile_hash": request["profile_hash"], "source_document_count": len(source)}
        budget_held = bool(budget_state and budget_state["status"] == "hold")
        if budget_held and status not in {"awaiting_approval", "approved", "rejected"}:
            result["automation_error"] = budget_state["reason"]
        if self.automation and not budget_held and not selected and status == "requested":
            try:
                outcome = self.automation.start_author(request_id)
                if outcome == "exhausted":
                    result["automation_error"] = "author_attempts_exhausted"
                elif isinstance(outcome, str) and outcome.startswith("budget_"):
                    result["automation_error"] = outcome
            except Exception:
                result["automation_error"] = "author_start_failed"
        if selected:
            candidate_id, _, validation_id, manifest, decision = selected
            result["candidate_id"] = candidate_id
            if validation_id: result["validation_id"] = validation_id
            if isinstance(manifest.get("code_sha256"), str) and _HASH.fullmatch(manifest["code_sha256"]): result["code_hash"] = manifest["code_sha256"]
            if status in {"awaiting_approval", "approved"} and isinstance(validation_id, str):
                try:
                    result["review"] = CandidateReviewService(self.storage).get_v3(
                        request_id, candidate_id, validation_id)
                    result["source_count"] = result["review"].get("source_count", len(source))
                    result["unique_fixture_count"] = result["review"].get("unique_fixture_count", 0)
                except CandidateReviewError as exc:
                    result["review_error"] = str(exc).lower()
                except ProcessingError:
                    result["review_error"] = "review_unavailable"
            if decision:
                for key in ("decision_id", "variant_id"):
                    if isinstance(decision.get(key), str): result[key] = decision[key]
                variant_version = decision.get("variant_version")
                legacy_version = decision.get("version")
                if isinstance(variant_version, str):
                    result["variant_version"] = variant_version
                elif isinstance(legacy_version, str) and not legacy_version.startswith("admin-decision."):
                    result["variant_version"] = legacy_version
            if self.automation and status == "generated_unvalidated":
                try:
                    self.automation.start_validator(request_id, candidate_id)
                except Exception:
                    result["automation_error"] = "validator_start_failed"
            if self.automation and not budget_held and status in {"generated_unvalidated", "schema_invalid"}:
                if status == "generated_unvalidated" and any(superseded_layout_failure(item)
                    for item in evidence.get(candidate_id, [])):
                    return result  # Revalidate corrected policy, never paid-refine old feedback.
                # Refinement loop: if validation already failed for this
                # candidate, re-author with the failure feedback. Attempt
                # markers inside the automation bound the number of retries.
                try:
                    failure_event = self._latest_validation_failure(request_id, candidate_id, validation_id)
                    if failure_event is None and status == "schema_invalid":
                        selected_evidence = evidence.get(candidate_id, [])
                        if any(item.get("failure_feedback_policy") == "candidate-failure-feedback.v1"
                               for item in selected_evidence):
                            result["automation_error"] = "validation_failure_requires_review"
                            return result
                        # Older validators did not write failure events for
                        # schema/semantic rejections. Synthesize one so the
                        # refinement loop can still proceed.
                        failure_event = {"failure": "SchemaError",
                                         "failure_message": "canonical_schema_invalid"}
                    if failure_event is not None and failure_event.get("repairable") is not False:
                        outcome = self.automation.start_author_refinement(request_id, candidate_id, failure_event)
                        if isinstance(outcome, str) and outcome.startswith("budget_"):
                            result["automation_error"] = outcome
                        elif outcome == "row_accounting_review_required":
                            # Plan 8.8 step 4b: 2 rounds spent; a person decides (locations only).
                            result["automation_error"] = outcome
                            result["row_accounting"] = failure_event["row_accounting"]
                    elif failure_event is not None and _manufacturer_retry_hint(failure_event) is not None:
                        # A047: makers and MPNs kept on another sheet get one automatic retry first.
                        outcome = self.automation.start_manufacturer_retry(request_id, candidate_id, failure_event)
                        if outcome in {"manufacturer_data_review_required", "exhausted"}:
                            result["automation_error"] = "manufacturer_data_review_required"
                        elif isinstance(outcome, str) and outcome.startswith("budget_"):
                            result["automation_error"] = outcome
                    elif failure_event is not None:
                        # Round 8: a BOM without any manufacturer/MPN pair is named, not a generic stop.
                        reason = failure_event.get("review_reason")
                        result["automation_error"] = (reason if reason == "manufacturer_data_review_required"
                                                      else "validation_failure_requires_review")
                except Exception:
                    result.setdefault("automation_error", "author_refinement_start_failed")
            elif not self.automation and not budget_held and status in {"generated_unvalidated", "schema_invalid"}:
                # A047: package review reads status without automation; a held file still says why.
                try:
                    held = self._held_reason(request_id, candidate_id, validation_id, status,
                                             evidence.get(candidate_id, []), manifest)
                except Exception:
                    held = None
                if held is not None:
                    result["automation_error"] = held[0]
                    if held[1] is not None:
                        result["row_accounting"] = held[1]
        return result

    def _held_reason(self, request_id: str, candidate_id: str, validation_id: str | None, status: str,
                     selected_evidence: list[dict[str, Any]],
                     manifest: dict[str, Any]) -> tuple[str, dict[str, Any] | None] | None:
        """The hold the automated path would report, read only: never starts a job or spends budget.

        None while automation would still act (a refinement or the A047 manufacturer retry to start or
        running), so such a file shows as in progress, not held.
        """
        from .validation_policy import superseded_layout_failure
        if status == "generated_unvalidated" and any(superseded_layout_failure(item) for item in selected_evidence):
            return None
        failure_event = self._latest_validation_failure(request_id, candidate_id, validation_id)
        if failure_event is None:
            if status == "schema_invalid" and any(item.get("failure_feedback_policy") == "candidate-failure-feedback.v1"
                                                  for item in selected_evidence):
                return "validation_failure_requires_review", None
            return None
        if failure_event.get("repairable") is False:
            reason = failure_event.get("review_reason")
            if reason == "manufacturer_data_review_required":
                from .manufacturer_lookup import retry_state
                if (_manufacturer_retry_hint(failure_event) is not None
                        and retry_state(self.storage, request_id, candidate_id) != "spent"):
                    return None
                return reason, None
            return "validation_failure_requires_review", None
        accounting = failure_event.get("row_accounting")
        if isinstance(accounting, dict) and isinstance(manifest.get("version"), str):
            from .candidate_validation import MAX_ROW_ACCOUNTING_ROUNDS
            from .row_refinement import HOLD_REASON, row_accounting_rounds
            rounds = row_accounting_rounds(self.storage, request_id, candidate_id, manifest["version"],
                                           lambda prefix: self.storage.list_registry_paths(prefix, self.max_results))
            if rounds >= MAX_ROW_ACCOUNTING_ROUNDS:
                return HOLD_REASON, accounting
        return None

    def _latest_validation_failure(self, request_id: str, candidate_id: str,
                                   validation_id: str | None = None) -> dict[str, Any] | None:
        """Return the most recent validation-failed event for a candidate, if any."""
        event_root = f"candidates/events/{request_id}/{candidate_id}/"
        try:
            paths = self.storage.list_candidate_paths(event_root, self.max_results)
        except Exception:
            return None
        failures = [p for p in paths if p.startswith(event_root) and "/validation-failed-" in p and p.endswith(".json")]
        if validation_id is not None:
            failures = [p for p in failures if p == f"{event_root}validation-failed-{validation_id}.json"]
        if not failures:
            return None
        failures.sort()
        try:
            record = _json(self.storage, failures[-1], True)
            if validation_id is not None and (record.get("request_id") != request_id
                    or record.get("candidate_id") != candidate_id or record.get("validation_id") != validation_id):
                return None
            return record
        except Exception:
            return None

    def _superseded_by_refinement(self, request_id: str) -> set[str]:
        """Candidate ids that a started author-refinement marker names as its source.

        Best effort: if the automation namespace cannot be listed or read, no
        candidate is treated as superseded and the caller falls back to the
        existing ambiguous-candidate error.
        """
        root = f"candidate-automation/{request_id}/author-refinement/"
        try:
            paths = self.storage.list_registry_paths(root, self.max_results)
        except Exception:
            return set()
        superseded: set[str] = set()
        for path in paths:
            if not path.endswith(".started.json"):
                continue
            try:
                marker = _json(self.storage, path, False)
            except Exception:
                continue
            source = marker.get("refinement_of")
            if isinstance(source, str) and _ID.fullmatch(source):
                superseded.add(source)
        return superseded
