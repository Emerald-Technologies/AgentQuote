"""Local candidate authoring job entrypoint; only REQUEST_ID is request-scoped."""
from __future__ import annotations
import json
import os

from bom_processor.candidate_authoring import AUTHORING_VERSION, ASSOCIATION_AUTHORING_VERSION


_ATTEMPT_KINDS = {"author", "author-refinement"}


def _attempt_context() -> tuple[str, int] | None:
    kind = os.environ.get("AUTHOR_ATTEMPT_KIND", "")
    raw_attempt = os.environ.get("AUTHOR_ATTEMPT", "")
    if not kind and not raw_attempt:
        return None
    try:
        attempt = int(raw_attempt)
    except ValueError:
        raise ValueError("invalid author attempt environment") from None
    if kind not in _ATTEMPT_KINDS or not 1 <= attempt <= 100:
        raise ValueError("invalid author attempt environment")
    return kind, attempt


def _write_attempt_marker(storage, request_id: str, context: tuple[str, int], state: str,
                          values: dict[str, object]) -> None:
    kind, attempt = context
    version = (ASSOCIATION_AUTHORING_VERSION if os.environ.get("BOM_SOURCE_ASSOCIATIONS") == "true"
           else AUTHORING_VERSION)
    path = (f"candidate-automation/{request_id}/{kind}/{version}/"
            f"attempt-{attempt}.{state}.json")
    marker = {"request_id": request_id, "job": kind.replace("-", "_"),
          "authoring_version": version, "attempt": attempt, **values}
    storage.upload_registry_immutable(
        path, json.dumps(marker, sort_keys=True).encode(), "application/json")


def main() -> int:
    request_id = os.environ.get("REQUEST_ID", "")
    unknown = sorted(key for key in os.environ if key.startswith("REQUEST_") and key != "REQUEST_ID")
    if not request_id or unknown:
        print(json.dumps({"status": "error", "error": "invalid_request_environment"}, sort_keys=True))
        return 2
    association_policy = os.environ.get("BOM_SOURCE_ASSOCIATIONS", "false")
    if association_policy not in {"true", "false"}:
        print(json.dumps({"status": "error", "error": "invalid_association_policy"}, sort_keys=True))
        return 2
    try:
        attempt_context = _attempt_context()
    except ValueError:
        print(json.dumps({"status": "error", "error": "invalid_author_attempt_environment",
                          "request_id": request_id}, sort_keys=True))
        return 2
    storage = None
    try:
        from azure.identity import ManagedIdentityCredential
        from bom_processor.core import AzureBlobStorage
        from bom_processor.candidate_authoring import CandidateAuthoringService
        from bom_processor.foundry_author import create_foundry_author
        client_id = os.environ.get("AZURE_CLIENT_ID")
        credential = (ManagedIdentityCredential(client_id=client_id)
                  if client_id else ManagedIdentityCredential())
        storage = AzureBlobStorage.from_account_url(os.environ["BOM_STORAGE_ACCOUNT_URL"], credential)
        budget_values = [os.environ.get(key, "") for key in
                         ("AUTHOR_BUDGET_GROUP", "AUTHOR_BUDGET_SLOT", "AUTHOR_BUDGET_RESERVATION")]
        if any(budget_values):
            from bom_processor.author_budget import begin_execution
            if not all(budget_values):
                raise ValueError("invalid author budget environment")
            budget_owner = begin_execution(storage, budget_values[0], int(budget_values[1]), request_id, budget_values[2])
        feedback = os.environ.get("AUTHOR_FEEDBACK", "") or None
        # Plan 8.8 step 4b: ids of a row-accounting failure; the service reloads and verifies both.
        refine_ids = (os.environ.get("AUTHOR_REFINE_CANDIDATE", ""), os.environ.get("AUTHOR_REFINE_VALIDATION", ""))
        if any(refine_ids) and not all(refine_ids):
            raise ValueError("invalid author refinement environment")
        extra = {"refine": refine_ids} if all(refine_ids) else {}
        # Round 12: an earlier try of this same fix was refused by the code policy.
        if os.environ.get("AUTHOR_POLICY_RETRY") == "true":
            extra["policy_retry"] = True
        # Only pass the feedback keyword when present so older injected doubles
        # that do not accept it continue to work unchanged.
        result = (CandidateAuthoringService(request_id, storage, create_foundry_author(), feedback=feedback,
              source_associations=True, **extra).author() if association_policy == "true" else
              CandidateAuthoringService(request_id, storage, create_foundry_author(), feedback=feedback, **extra).author()
                  if feedback or extra else CandidateAuthoringService(request_id, storage, create_foundry_author()).author())
        if attempt_context:
            _write_attempt_marker(storage, request_id, attempt_context, "completed",
                                  {"candidate_id": result.candidate_id})
        if any(budget_values):
            from bom_processor.author_budget import complete
            complete(storage, budget_values[0], int(budget_values[1]), request_id,
                     budget_values[2], "completed", result.candidate_id, execution_owner=budget_owner)
        print(json.dumps({"status": result.status, "request_id": result.request_id,
                          "candidate_id": result.candidate_id, "manifest_path": result.manifest_path}, sort_keys=True))
        return 0
    except Exception as exc:
        error_code = None
        rule_id = None
        diagnostics = {}
        try:
            from bom_processor.candidate_authoring import AuthoringError
            if isinstance(exc, AuthoringError):
                error = "candidate_authoring_failed"
                error_code = exc.code
                rule_id = exc.rule_id
                diagnostics = exc.diagnostics
            else:
                error = "candidate_job_failed"
        except Exception:
            error = "candidate_job_failed"
        if storage is not None and attempt_context:
            marker = {"error_type": type(exc).__name__, **diagnostics}
            if error_code: marker["error_code"] = error_code
            if rule_id: marker["rule_id"] = rule_id
            try:
                _write_attempt_marker(storage, request_id, attempt_context, "failed", marker)
            except Exception:
                pass
        if storage is not None and 'budget_owner' in locals() and all(budget_values):
            try:
                from bom_processor.author_budget import complete
                complete(storage, budget_values[0], int(budget_values[1]), request_id,
                         budget_values[2], "failed", execution_owner=budget_owner)
            except Exception:
                pass
        payload = {"status": "error", "request_id": request_id, "error": error, **diagnostics}
        if error_code: payload["error_code"] = error_code
        if rule_id: payload["rule_id"] = rule_id
        print(json.dumps(payload, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
