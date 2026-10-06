"""Opt-in, artifact-aware starts for the candidate controller Jobs."""
from __future__ import annotations

import json
import copy
from datetime import datetime, timezone
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

    def __init__(self, storage: Any, author_starter: Any = None, validator_starter: Any = None,
                 max_author_attempts: int | None = None, *, author_budget_enabled: bool = False,
                 association_quote_ids: frozenset[str] = frozenset(),
                 budget_clock=None):
        self.storage, self.author_starter, self.validator_starter = storage, author_starter, validator_starter
        self.author_budget_enabled = author_budget_enabled
        from .quote_budget_scope import parse_quote_scope
        self.association_quote_ids = parse_quote_scope(",".join(sorted(association_quote_ids)))
        self._request_policy_resolved = False
        self.budget_clock = budget_clock or (lambda: datetime.now(timezone.utc))
        policy = getattr(author_starter, "base_environment", {}).get("BOM_SOURCE_ASSOCIATIONS", "false")
        if policy not in {"true", "false"}:
            raise ValueError("invalid association policy")
        from .candidate_authoring import ASSOCIATION_AUTHORING_VERSION
        self.authoring_version = ASSOCIATION_AUTHORING_VERSION if policy == "true" else AUTHORING_VERSION
        self.author_environment = {"BOM_SOURCE_ASSOCIATIONS": "true"} if policy == "true" else {}
        if max_author_attempts is not None:
            if type(max_author_attempts) is not int or max_author_attempts < 1:
                raise ValueError("invalid author attempt limit")
            self.MAX_AUTHOR_ATTEMPTS = max_author_attempts

    def _paths(self, prefix: str) -> list[str]:
        if prefix.startswith("candidates/"):
            return self.storage.list_candidate_paths(prefix, 100)
        return self.storage.list_registry_paths(prefix, 100)

    def _refinement_sources(self, request_id: str) -> set[str]:
        """Candidate ids that started author-refinement markers name as their source."""
        root = f"candidate-automation/{request_id}/author-refinement/"
        sources: set[str] = set()
        try:
            paths = self._paths(root)
        except Exception:
            return sources
        for path in paths:
            if not path.endswith(".started.json"):
                continue
            marker = self._read_marker(path)
            source = (marker or {}).get("refinement_of")
            if isinstance(source, str) and source:
                sources.add(source)
        return sources

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
        # Terminal evidence must win over the earlier started marker. Both are
        # immutable, so a completed or failed job necessarily retains started.
        for state in ("completed", "failed", "started"):
            if self._marker_exists(f"{prefix}/attempt-{attempt}.{state}.json"):
                return state
        return "claimed" if self._marker_exists(f"{prefix}/attempt-{attempt}.claim.json") else "vacant"

    def _run_attempt(self, *, prefix: str, values: Mapping[str, str], marker_values: Mapping[str, Any],
                     starter: Any, max_attempts: int | None = None,
                     author_attempt_kind: str | None = None) -> str | None:
        limit = max_attempts if max_attempts is not None else self.MAX_ATTEMPTS
        for attempt in range(1, limit + 1):
            state = self._attempt_state(prefix, attempt)
            if state == "started":
                return None
            if state in {"failed", "completed"}:
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
                start_values = dict(values)
                if author_attempt_kind:
                    start_values.update({
                        "AUTHOR_ATTEMPT": str(attempt),
                        "AUTHOR_ATTEMPT_KIND": author_attempt_kind,
                    })
                execution_name = starter.start(start_values)
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
        return "exhausted"

    def start_author(self, request_id: str) -> str | None:
        if self.author_starter is None:
            return None
        from .package_writer_gate import candidate_writer
        # Fence eligibility and dispatch share the same short writer. A check
        # outside this scope could race a repair coordinator arming its fence.
        with candidate_writer(self.storage, request_id, "recovery"):
            return self._request_policy(request_id)._start_author_locked(request_id)

    def _start_author_locked(self, request_id: str) -> str | None:
        if self._budget_required(request_id):
            from .author_budget import Budget
            budget = Budget(self.storage, request_id, clock=self.budget_clock)
            state = budget.inspect()
            if state["status"] != "legacy":
                root = f"candidates/{request_id}/"
                if any(path.endswith("/candidate-manifest.json") for path in self._paths(root)):
                    return None
                return self._budget_start(request_id, "author")
        root = f"candidates/{request_id}/"
        for path in self._paths(root):
            if not path.endswith("/candidate-manifest.json"):
                continue
            try:
                manifest = json.loads(self.storage.download_candidate(path, 1_000_000).decode("utf-8"))
            except Exception:
                continue
            if manifest.get("version") == self.authoring_version:
                return None
        # Version the namespace too: immutable v2 and v3 attempts coexist.
        root = f"candidate-automation/{request_id}/author/{self.authoring_version}"
        # Legacy start-only markers cannot prove that an execution completed.
        # A failed execution previously left this request permanently blocked,
        # so only a current-version candidate artifact or a new attempt record
        # suppresses a start.
        result = self._run_attempt(
            prefix=root, values={"REQUEST_ID": request_id, **self.author_environment},
            marker_values={"request_id": request_id, "job": "author", "authoring_version": self.authoring_version},
            starter=self.author_starter, author_attempt_kind="author")
        if result == "started":
            # Retain the old marker shape for readers deployed before attempt records.
            try:
                self._write_marker(f"candidate-automation/{request_id}/author-started-{self.authoring_version}.json",
                                   {"request_id": request_id, "job": "author", "authoring_version": self.authoring_version})
            except Exception:
                pass
        return result

    def start_validator(self, request_id: str, candidate_id: str) -> str | None:
        if self.validator_starter is None:
            return None
        from .package_writer_gate import candidate_writer
        from .serial_validation import reserve, SerialValidationHeld
        # The persistent slot spans remote execution; the short writer must not
        # span ARM dispatch because the validator needs that writer on startup.
        from .author_budget import _membership
        binding = _membership(self.storage, request_id)
        from .semantic_repair import read_fence
        fence = read_fence(self.storage, binding[0]["plan_id"])[0] if binding else None
        if binding is not None and fence is None:
            with candidate_writer(self.storage, request_id, "validation", candidate_id=candidate_id):
                root = f"candidates/{request_id}/{candidate_id}/"
                if any("/validation/" in path and path.endswith("/evidence.json") for path in self._paths(root)):
                    return None
                attempt_root = f"candidate-automation/{request_id}/{candidate_id}/validator/{AUTHORING_VERSION}"
                # Started/claimed history is never converted into a fresh slot.
                # Parser failure feedback drives author refinement, not revalidation.
                if self._paths(attempt_root + "/"):
                    return None
                try:
                    owner = reserve(self.storage, request_id, candidate_id)
                except SerialValidationHeld:
                    return None
                except Exception:
                    return None  # Conditional conflict/uncertainty never permits dispatch.
            # Any exception/unknown result retains the slot; no retry or stealing.
            return self._start_validator_locked(request_id, candidate_id, serial_owner=owner)
        with candidate_writer(self.storage, request_id, "validation", candidate_id=candidate_id):
            return self._start_validator_locked(request_id, candidate_id)

    def _start_validator_locked(self, request_id: str, candidate_id: str, *, serial_owner=None) -> str | None:
        root = f"candidates/{request_id}/{candidate_id}/"
        paths = self._paths(root)
        if any("/validation/" in path and path.endswith("/evidence.json") for path in paths):
            return None
        root = f"candidate-automation/{request_id}/{candidate_id}/validator/{AUTHORING_VERSION}"
        # Legacy start-only markers are not completion evidence; validation
        # evidence or the versioned attempt records above provide suppression.
        result = self._run_attempt(
            prefix=root, values={"REQUEST_ID": request_id, "CANDIDATE_ID": candidate_id,
                                 **({"BOM_VALIDATOR_SERIAL_OWNER": serial_owner} if serial_owner else {})},
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
        if self.author_starter is None:
            return None
        from .package_writer_gate import candidate_writer
        with candidate_writer(self.storage, request_id, "recovery", candidate_id=candidate_id):
            return self._request_policy(request_id)._start_author_refinement_locked(request_id, candidate_id, failure_event)

    def _start_author_refinement_locked(self, request_id: str, candidate_id: str,
                                        failure_event: dict[str, Any] | None = None) -> str | None:
        """Re-trigger authoring with feedback after a validation failure.

        Uses a separate attempt namespace from the initial authoring so the
        original attempt markers do not suppress refinement starts. Each
        refinement attempt gets its own claim/started markers.

        Returns "started" if a new authoring job was launched, None otherwise.
        """
        if self.author_starter is None:
            return None
        if self._budget_required(request_id):
            from .author_budget import Budget
            state = Budget(self.storage, request_id, clock=self.budget_clock).inspect()
            if state["status"] != "legacy":
                return self._budget_start(request_id, "author-refinement", candidate_id, failure_event)

        # Check if a newer candidate already exists (avoid duplicate refinement).
        # A candidate that a started refinement marker names as its source is
        # superseded, not newer, so it must not block a further refinement.
        superseded = self._refinement_sources(request_id)
        root = f"candidates/{request_id}/"
        for path in self._paths(root):
            if not path.endswith("/candidate-manifest.json"):
                continue
            try:
                manifest = json.loads(self.storage.download_candidate(path, 1_000_000).decode("utf-8"))
            except Exception:
                continue
            other_id = manifest.get("candidate_id")
            if (other_id != candidate_id and other_id not in superseded
                    and manifest.get("version") == self.authoring_version):
                return None  # A newer candidate already exists.

        # Count existing refinement attempts (separate namespace from initial authoring).
        # Count only started markers: each attempt writes both a claim and a
        # started marker, so counting both would double the budget. A claim
        # without a started marker represents an in-flight attempt, which
        # _run_attempt treats as occupied rather than consumed.
        refinement_root = f"candidate-automation/{request_id}/author-refinement/{self.authoring_version}"
        existing_attempts = sum(
            1 for path in self._paths(refinement_root + "/")
            if path.endswith(".started.json")
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
        values = {"REQUEST_ID": request_id, **self.author_environment}
        if feedback:
            values["AUTHOR_FEEDBACK"] = feedback

        result = self._run_attempt(
            prefix=refinement_root, values=values,
            marker_values={"request_id": request_id, "job": "author_refinement",
                           "authoring_version": self.authoring_version,
                           "refinement_of": candidate_id},
            starter=self.author_starter,
            max_attempts=self.MAX_AUTHOR_ATTEMPTS,
            # The author job writes terminal evidence. A started marker alone
            # remains in flight and must not fan out concurrent refinements.
            author_attempt_kind="author-refinement")
        return result

    def _request_policy(self, request_id: str):
        """Resolve from trusted membership, never mutate shared dispatcher state."""
        if self._request_policy_resolved:
            return self
        from .core import BlobNotFoundError
        try:
            self.storage.download_registry(f"author-budget/v1/members/{request_id}.json", 100_000)
        except (BlobNotFoundError, FileNotFoundError):
            return self
        from .author_budget import _membership
        binding = _membership(self.storage, request_id)
        if binding is None:
            return self  # Unenrolled requests cannot opt in via an untrusted path.
        from .candidate_authoring import ASSOCIATION_AUTHORING_VERSION
        selected = (self.authoring_version == ASSOCIATION_AUTHORING_VERSION
                    or binding[1]["identity"]["rfq_uuid"] in self.association_quote_ids)
        from .author_policy import resolve
        policy = resolve(self.storage, request_id, selected)
        scoped = copy.copy(self)
        scoped._request_policy_resolved = True
        scoped.authoring_version = policy["authoring_version"]
        scoped.author_environment = {"BOM_SOURCE_ASSOCIATIONS": "true" if policy["source_associations"] else "false"}
        return scoped

    def _budget_required(self, request_id: str) -> bool:
        if self.author_budget_enabled:
            return True
        from .core import BlobNotFoundError
        for kind in ("members", "required"):
            try:
                self.storage.download_registry(f"author-budget/v1/{kind}/{request_id}.json", 100_000)
                return True
            except (BlobNotFoundError, FileNotFoundError):
                continue
        return False

    def _budget_start(self, request_id: str, phase: str, candidate_id: str | None = None,
                      failure_event: dict[str, Any] | None = None) -> str:
        from .author_budget import Budget
        from .core import _json_bytes, _sha256
        budget = Budget(self.storage, request_id, clock=self.budget_clock)
        if phase == "author-refinement":
            try:
                if budget.selected_candidate() not in (None, candidate_id):
                    return "budget_no_progress"
            except Exception:
                return "budget_invalid"
        failure_hash = _sha256(_json_bytes(failure_event)) if phase == "author-refinement" else None
        reservation = budget.reserve(phase, candidate_id, failure_hash)
        if reservation["status"] != "reserved":
            return reservation["reason"]
        values = {"REQUEST_ID": request_id, **self.author_environment,
                  "AUTHOR_BUDGET_GROUP": reservation["group_id"],
                  "AUTHOR_BUDGET_SLOT": str(reservation["slot"]),
                  "AUTHOR_BUDGET_RESERVATION": reservation["reservation_hash"]}
        if (phase == "author" and reservation["slot"] > 1
                and self.author_environment.get("BOM_SOURCE_ASSOCIATIONS") == "true"):
            # Budget.reserve has verified preceding terminal history and absence
            # of a completed candidate. This is a new counted attempt, not an
            # inline second model call or a change to the pinned author policy.
            from .candidate_authoring import _BANNED_MODULES
            values["AUTHOR_FEEDBACK"] = (
                "The preceding author attempt failed before publishing a candidate. "
                "Follow the enforced import policy: do not import "
                + ", ".join(sorted(_BANNED_MODULES))
                + ". Use argparse for input/output arguments, pathlib or open for local paths, "
                "and pandas/openpyxl for workbook parsing; no network, subprocess or dynamic imports. "
                "This guidance does not identify the previous failure's exact import."
            )
        if failure_event:
            values["AUTHOR_FEEDBACK"] = (f"Previous parser failed with {failure_event.get('failure') or failure_event.get('error_code')}: "
                                        f"{failure_event.get('failure_message') or ''}")[:1000]
        try:
            budget.mark_dispatch(reservation)
        except Exception:
            return "budget_invalid"
        try:
            execution = self.author_starter.start(values)
            budget.mark_started(reservation, execution)
        except Exception:
            try:
                budget.mark_unknown(reservation)
            except Exception:
                pass  # Existing claim still blocks another dispatch.
            return "budget_start_unknown"
        return "started"
