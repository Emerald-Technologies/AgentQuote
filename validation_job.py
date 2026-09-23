"""Candidate validation controller; generated parser code runs only in the worker Job."""
from __future__ import annotations

import json
import os


def main() -> int:
    request_id = os.environ.get("REQUEST_ID", "")
    candidate_id = os.environ.get("CANDIDATE_ID", "")
    allowed = {"REQUEST_ID", "CANDIDATE_ID"}
    unknown = sorted(key for key in os.environ
                     if key.startswith(("REQUEST_", "CANDIDATE_")) and key not in allowed)
    if not request_id or not candidate_id or unknown:
        print(json.dumps({"status": "error", "error": "invalid_request_environment"}, sort_keys=True))
        return 2
    try:
        from azure.identity import ManagedIdentityCredential

        from bom_processor.candidate_validation import CandidateValidationService
        from bom_processor.core import AzureBlobStorage, ProcessingError
        from bom_processor.job_orchestrator import AzureJobStarter, AzureSasFactory, JobOrchestrator

        client_id = os.environ.get("AZURE_CLIENT_ID", "")
        credential = (ManagedIdentityCredential(client_id=client_id)
                      if client_id else ManagedIdentityCredential())
        account_url = os.environ["BOM_STORAGE_ACCOUNT_URL"]
        storage = AzureBlobStorage.from_account_url(account_url, credential)
        max_source = int(os.environ.get("BOM_MAX_SOURCE_BYTES", "50000000"))
        max_parser = int(os.environ.get("BOM_MAX_PARSER_BYTES", "1000000"))

        class Reader:
            def read(self, container: str, blob: str) -> bytes:
                if container == "boms":
                    return storage.download_source(blob, max_source)
                if container == "parsers" and blob.startswith("candidates/"):
                    return storage.download_candidate(blob, max_parser)
                raise ProcessingError("job storage path invalid")

        subscription = os.environ["BOM_JOB_SUBSCRIPTION_ID"]
        resource_group = os.environ["BOM_JOB_RESOURCE_GROUP"]
        worker_job = os.environ["BOM_JOB_NAME"]
        worker_image = os.environ["BOM_WORKER_IMAGE"]
        starter = AzureJobStarter(
            subscription_id=subscription, resource_group=resource_group,
            job_name=worker_job, credential=credential,
            container_name=os.environ.get("BOM_JOB_CONTAINER_NAME", "synthetic-parser-worker"),
            template={"image": worker_image, "resources": {
                "cpu": float(os.environ.get("BOM_WORKER_CPU", "0.5")),
                "memory": os.environ.get("BOM_WORKER_MEMORY", "1Gi")}},
        )
        service = CandidateValidationService(
            request_id, candidate_id, storage,
            JobOrchestrator(Reader(), AzureSasFactory(account_url, credential), starter),
            timeout_seconds=int(os.environ.get("BOM_PROCESS_TIMEOUT_SECONDS", "300")),
            poll_interval_seconds=int(os.environ.get("BOM_POLL_INTERVAL_SECONDS", "2")),
            max_source_bytes=max_source, max_parser_bytes=max_parser,
            max_output_bytes=int(os.environ.get("BOM_MAX_OUTPUT_BYTES", "50000000")),
            max_rows=int(os.environ.get("BOM_MAX_ROWS", "100000")),
            job_resource=(f"/subscriptions/{subscription}/resourceGroups/{resource_group}/"
                          f"providers/Microsoft.App/jobs/{worker_job}"),
            worker_image=worker_image,
        )
        result = service.validate()
        print(json.dumps({"status": result.status, "request_id": result.request_id,
                          "candidate_id": result.candidate_id,
                          "validation_id": result.validation_id,
                          "evidence_path": result.evidence_path}, sort_keys=True))
        return 0
    except Exception as exc:
        try:
            from bom_processor.candidate_validation import CandidateValidationError
            error = exc.code if isinstance(exc, CandidateValidationError) else type(exc).__name__
        except Exception:
            error = type(exc).__name__
        print(json.dumps({"status": "error", "request_id": request_id,
                          "candidate_id": candidate_id, "error": error}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())