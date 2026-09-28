"""Bounded, fail-closed status projection for one candidate request."""
from __future__ import annotations

import json
import re
from typing import Any

from .core import ProcessingError

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
        for candidate_id, manifest in manifests.items():
            if manifest.get("status") not in {"generated_unvalidated"}:
                raise CandidateStatusError("invalid candidate artifact")
            ev = evidence.get(candidate_id, [])
            if len(ev) > 1:
                raise CandidateStatusError("ambiguous validation evidence")
            current = "generated_unvalidated"
            validation_id = None
            if ev:
                validation_id = ev[0].get("validation_id")
                current = "awaiting_approval" if ev[0].get("schema", {}).get("valid") is True else "schema_invalid"
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
        if self.automation and not selected and status == "requested":
            try:
                if self.automation.start_author(request_id) == "exhausted":
                    result["automation_error"] = "author_attempts_exhausted"
            except Exception:
                result["automation_error"] = "author_start_failed"
        if selected:
            candidate_id, _, validation_id, manifest, decision = selected
            result["candidate_id"] = candidate_id
            if validation_id: result["validation_id"] = validation_id
            if isinstance(manifest.get("code_sha256"), str) and _HASH.fullmatch(manifest["code_sha256"]): result["code_hash"] = manifest["code_sha256"]
            if decision:
                for key in ("decision_id", "variant_id", "version"):
                    if isinstance(decision.get(key), str): result[key] = decision[key]
            if self.automation and status == "generated_unvalidated":
                try:
                    self.automation.start_validator(request_id, candidate_id)
                except Exception:
                    result["automation_error"] = "validator_start_failed"
            if self.automation and status in {"generated_unvalidated", "schema_invalid"}:
                # Refinement loop: if validation already failed for this
                # candidate, re-author with the failure feedback. Attempt
                # markers inside the automation bound the number of retries.
                try:
                    failure_event = self._latest_validation_failure(request_id, candidate_id)
                    if failure_event is None and status == "schema_invalid":
                        # Older validators did not write failure events for
                        # schema/semantic rejections. Synthesize one so the
                        # refinement loop can still proceed.
                        failure_event = {"failure": "SchemaError",
                                         "failure_message": "canonical_schema_invalid"}
                    if failure_event is not None:
                        self.automation.start_author_refinement(request_id, candidate_id, failure_event)
                except Exception:
                    result.setdefault("automation_error", "author_refinement_start_failed")
        return result

    def _latest_validation_failure(self, request_id: str, candidate_id: str) -> dict[str, Any] | None:
        """Return the most recent validation-failed event for a candidate, if any."""
        event_root = f"candidates/events/{request_id}/{candidate_id}/"
        try:
            paths = self.storage.list_candidate_paths(event_root, self.max_results)
        except Exception:
            return None
        failures = [p for p in paths if p.startswith(event_root) and "/validation-failed-" in p and p.endswith(".json")]
        if not failures:
            return None
        failures.sort()
        try:
            return _json(self.storage, failures[-1], True)
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
