"""Opt-in, artifact-aware starts for the candidate controller Jobs."""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Mapping

from .candidate_authoring import AUTHORING_VERSION


class AzureCandidateJobStarter:
    """Start a Container Apps Job without passing storage credentials or SAS."""
    def __init__(self, *, subscription_id: str, resource_group: str, job_name: str,
                 credential: Any, container_name: str, image: str,
                 base_environment: Mapping[str, str] | None = None,
                 resources: Mapping[str, Any] | None = None,
                 api_version: str = "2024-03-01", opener: Any = urllib.request.urlopen):
        self.subscription_id, self.resource_group, self.job_name = subscription_id, resource_group, job_name
        self.credential, self.container_name, self.image = credential, container_name, image
        self.base_environment = dict(base_environment or {})
        self.resources, self.api_version, self.opener = dict(resources or {}), api_version, opener

    def start(self, values: Mapping[str, str]) -> str:
        token = self.credential.get_token("https://management.usgovcloudapi.net/.default").token
        endpoint = (f"https://management.usgovcloudapi.net/subscriptions/{self.subscription_id}"
                    f"/resourceGroups/{self.resource_group}/providers/Microsoft.App/jobs/{self.job_name}"
                    f"/start?api-version={self.api_version}")
        environment = {**self.base_environment, **values}
        container = {"name": self.container_name, "image": self.image,
                 "resources": self.resources,
                 "env": [{"name": key, "value": environment[key]} for key in sorted(environment)]}
        request = urllib.request.Request(endpoint, data=json.dumps({"containers": [container]}, separators=(",", ":")).encode(),
                                         headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}, method="POST")
        try:
            with self.opener(request) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (OSError, urllib.error.HTTPError, ValueError) as exc:
            raise RuntimeError("candidate job start failed") from exc
        name = payload.get("name") or payload.get("properties", {}).get("executionName")
        if not isinstance(name, str) or not name:
            raise RuntimeError("candidate job start returned no execution name")
        return name.rsplit("/", 1)[-1]


class CandidateAutomationService:
    MAX_ATTEMPTS = 3

    def __init__(self, storage: Any, author_starter: Any = None, validator_starter: Any = None):
        self.storage, self.author_starter, self.validator_starter = storage, author_starter, validator_starter

    def _paths(self, prefix: str) -> list[str]:
        if prefix.startswith("candidates/"):
            return self.storage.list_candidate_paths(prefix, 100)
        return self.storage.list_registry_paths(prefix, 100)

    def _marker_exists(self, path: str) -> bool:
        try:
            self.storage.download_registry(path, 100_000)
            return True
        except Exception:
            return False

    def _write_marker(self, path: str, values: Mapping[str, str]) -> None:
        self.storage.upload_registry_immutable(path, json.dumps(dict(values), sort_keys=True).encode(), "application/json")

    def _read_marker(self, path: str) -> dict[str, Any] | None:
        try:
            value = json.loads(self.storage.download_registry(path, 100_000).decode("utf-8"))
        except Exception:
            return None
        return value if isinstance(value, dict) else None

    def _claim_attempt(self, prefix: str, values: Mapping[str, Any], attempt: int) -> bool:
        """Claim one immutable attempt; a conflict means another worker owns it."""
        path = f"{prefix}/attempt-{attempt}.claim.json"
        try:
            self._write_marker(path, {key: str(value) for key, value in values.items()})
            return True
        except Exception:
            return False

    def _attempt_state(self, prefix: str, attempt: int) -> str:
        for state in ("started", "failed"):
            if self._marker_exists(f"{prefix}/attempt-{attempt}.{state}.json"):
                return state
        return "claimed" if self._marker_exists(f"{prefix}/attempt-{attempt}.claim.json") else "vacant"

    def _run_attempt(self, *, prefix: str, values: Mapping[str, str], marker_values: Mapping[str, Any],
                     starter: Any) -> str | None:
        for attempt in range(1, self.MAX_ATTEMPTS + 1):
            state = self._attempt_state(prefix, attempt)
            if state == "started":
                return None
            if state == "failed":
                continue
            if state == "claimed":
                # An unpaired claim is deliberately treated as in-flight.  Do not
                # steal it from a concurrent worker or turn a stale start into a
                # fan-out of duplicate jobs.
                return None
            if not self._claim_attempt(prefix, {**marker_values, "attempt": attempt}, attempt):
                if self._attempt_state(prefix, attempt) == "failed":
                    continue
                return None
            try:
                execution_name = starter.start(values)
            except Exception as exc:
                # Persist only a bounded, non-sensitive failure classification.
                try:
                    self._write_marker(f"{prefix}/attempt-{attempt}.failed.json", {
                        **marker_values, "attempt": attempt, "error_type": type(exc).__name__,
                    })
                except Exception:
                    pass
                raise
            started = {
                **marker_values, "attempt": attempt,
                "execution_name": execution_name if isinstance(execution_name, str) else None,
            }
            self._write_marker(f"{prefix}/attempt-{attempt}.started.json", started)
            return "started"
        return None

    def start_author(self, request_id: str) -> str | None:
        if self.author_starter is None:
            return None
        root = f"candidates/{request_id}/"
        for path in self._paths(root):
            if not path.endswith("/candidate-manifest.json"):
                continue
            try:
                manifest = json.loads(self.storage.download_candidate(path, 1_000_000).decode("utf-8"))
            except Exception:
                continue
            if manifest.get("version") == AUTHORING_VERSION:
                return None
        # Version the namespace too: immutable v2 and v3 attempts coexist.
        root = f"candidate-automation/{request_id}/author/{AUTHORING_VERSION}"
        # Legacy start-only markers cannot prove that an execution completed.
        # A failed execution previously left this request permanently blocked,
        # so only a current-version candidate artifact or a new attempt record
        # suppresses a start.
        result = self._run_attempt(
            prefix=root, values={"REQUEST_ID": request_id},
            marker_values={"request_id": request_id, "job": "author", "authoring_version": AUTHORING_VERSION},
            starter=self.author_starter)
        if result == "started":
            # Retain the old marker shape for readers deployed before attempt records.
            try:
                self._write_marker(f"candidate-automation/{request_id}/author-started-{AUTHORING_VERSION}.json",
                                   {"request_id": request_id, "job": "author", "authoring_version": AUTHORING_VERSION})
            except Exception:
                pass
        return result

    def start_validator(self, request_id: str, candidate_id: str) -> str | None:
        if self.validator_starter is None:
            return None
        root = f"candidates/{request_id}/{candidate_id}/"
        paths = self._paths(root)
        if any("/validation/" in path and path.endswith("/evidence.json") for path in paths):
            return None
        root = f"candidate-automation/{request_id}/{candidate_id}/validator/{AUTHORING_VERSION}"
        # Legacy start-only markers are not completion evidence; validation
        # evidence or the versioned attempt records above provide suppression.
        result = self._run_attempt(
            prefix=root, values={"REQUEST_ID": request_id, "CANDIDATE_ID": candidate_id},
            marker_values={"request_id": request_id, "candidate_id": candidate_id,
                           "job": "validator", "authoring_version": AUTHORING_VERSION},
            starter=self.validator_starter)
        if result == "started":
            try:
                self._write_marker(f"candidate-automation/{request_id}/{candidate_id}-validator-started.json",
                                   {"request_id": request_id, "candidate_id": candidate_id, "job": "validator",
                                    "authoring_version": AUTHORING_VERSION})
            except Exception:
                pass
        return result

    MAX_AUTHOR_ATTEMPTS = 3

    def start_author_refinement(self, request_id: str, candidate_id: str,
                                failure_event: dict[str, Any] | None = None) -> str | None:
        """Re-trigger authoring with feedback after a validation failure.

        Uses a separate attempt namespace from the initial authoring so the
        original attempt markers do not suppress refinement starts. Each
        refinement attempt gets its own claim/started markers.

        Returns "started" if a new authoring job was launched, None otherwise.
        """
        if self.author_starter is None:
            return None

        # Check if a newer candidate already exists (avoid duplicate refinement).
        root = f"candidates/{request_id}/"
        for path in self._paths(root):
            if not path.endswith("/candidate-manifest.json"):
                continue
            try:
                manifest = json.loads(self.storage.download_candidate(path, 1_000_000).decode("utf-8"))
            except Exception:
                continue
            if manifest.get("candidate_id") != candidate_id and manifest.get("version") == AUTHORING_VERSION:
                return None  # A newer candidate already exists.

        # Count existing refinement attempts (separate namespace from initial authoring).
        refinement_root = f"candidate-automation/{request_id}/author-refinement/{AUTHORING_VERSION}"
        existing_attempts = sum(
            1 for path in self._paths(refinement_root + "/")
            if path.endswith(".started.json") or path.endswith(".claim.json")
        )
        if existing_attempts >= self.MAX_AUTHOR_ATTEMPTS:
            return None  # Exhausted.

        # Build feedback from the validation-failed event.
        feedback = ""
        if failure_event and isinstance(failure_event, dict):
            error_type = failure_event.get("failure") or failure_event.get("error_code") or "unknown"
            error_message = failure_event.get("failure_message") or ""
            feedback = f"Previous parser failed with {error_type}: {error_message}"

        # Trigger re-authoring with feedback.
        values = {"REQUEST_ID": request_id}
        if feedback:
            values["AUTHOR_FEEDBACK"] = feedback

        result = self._run_attempt(
            prefix=refinement_root, values=values,
            marker_values={"request_id": request_id, "job": "author_refinement",
                           "authoring_version": AUTHORING_VERSION,
                           "refinement_of": candidate_id},
            starter=self.author_starter)
        return result