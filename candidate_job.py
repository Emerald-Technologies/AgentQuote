"""Local candidate authoring job entrypoint; only REQUEST_ID is request-scoped."""
from __future__ import annotations
import json
import os


def main() -> int:
    request_id = os.environ.get("REQUEST_ID", "")
    unknown = sorted(key for key in os.environ if key.startswith("REQUEST_") and key != "REQUEST_ID")
    if not request_id or unknown:
        print(json.dumps({"status": "error", "error": "invalid_request_environment"}, sort_keys=True))
        return 2
    try:
        from azure.identity import ManagedIdentityCredential
        from bom_processor.core import AzureBlobStorage
        from bom_processor.candidate_authoring import CandidateAuthoringService
        from bom_processor.foundry_author import create_foundry_author
        client_id = os.environ.get("AZURE_CLIENT_ID")
        credential = (ManagedIdentityCredential(client_id=client_id)
                  if client_id else ManagedIdentityCredential())
        storage = AzureBlobStorage.from_account_url(os.environ["BOM_STORAGE_ACCOUNT_URL"], credential)
        result = CandidateAuthoringService(request_id, storage, create_foundry_author()).author()
        print(json.dumps({"status": result.status, "request_id": result.request_id,
                          "candidate_id": result.candidate_id, "manifest_path": result.manifest_path}, sort_keys=True))
        return 0
    except Exception as exc:
        try:
            from bom_processor.candidate_authoring import AuthoringError
            error = "candidate_authoring_failed" if isinstance(exc, AuthoringError) else "candidate_job_failed"
        except Exception:
            error = "candidate_job_failed"
        print(json.dumps({"status": "error", "request_id": request_id,
                          "error": error}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())