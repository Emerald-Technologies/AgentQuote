"""Approved exact A022 read-only Blob eligibility probe; no automation or writes.

Inputs: existing Government CLI identity, fixed request/group/plan. Output: hashes
and eligibility metadata only. Run with existing QuickQuote .venv Python.
"""
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
REQUEST = "7dba9e5e1c5da6af86cd99c2890330dacc66f69785e6735988992963d58bf415"
GROUP = "befb8964b6aed7b7888dc93b7583fe5bf1749d76faa79813f2fdbafc32a61a96"
PLAN = "fefb9c15879c7c7e956487d1fa4b2898370368ad0e34566baa34d94ccb584ff1"


def main():
    from azure.core.credentials import AccessToken
    from azure.storage.blob import BlobServiceClient
    from bom_processor.core import AzureBlobStorage
    from bom_processor.author_budget import Budget, _hash, _read
    from bom_processor.candidate_status import CandidateStatusService
    cli = lambda args: json.loads(subprocess.check_output(["az.cmd", *args], shell=True, timeout=30))
    context = cli(["account", "show", "-o", "json"])
    assert context["id"] == "37d47e73-56ae-433b-a340-86eac088c78e" and context["tenantId"] == "a6664873-a1e6-4a7e-9d4b-de20bfa7dbd6"
    assert cli(["cloud", "show", "-o", "json"])["name"] == "AzureUSGovernment"
    response = cli(["account", "get-access-token", "--resource", "https://storage.azure.com/", "-o", "json"])
    token = AccessToken(response["accessToken"], int(response["expires_on"]))
    class Credential:
        def get_token(self, *args, **kwargs):
            return token
    service = BlobServiceClient("https://etbomstoragegov.blob.core.usgovcloudapi.net", credential=Credential(), retry_total=0, connection_timeout=20, read_timeout=20)
    adapter = AzureBlobStorage.__new__(AzureBlobStorage)
    adapter._parsers = service.get_container_client("parsers")
    adapter._sources = service.get_container_client("boms")
    class ReadOnly:
        def __getattr__(self, name):
            if name.startswith(("upload", "delete")):
                raise AssertionError("WRITE_FORBIDDEN")
            return getattr(adapter, name)
    storage = ReadOnly()
    try:
        budget = Budget(storage, REQUEST)
        state = budget.inspect()
        claim = _read(storage, f"author-budget/v1/groups/{GROUP}/slots/2.claim.json")
        terminal = _read(storage, f"author-budget/v1/groups/{GROUP}/slots/2.terminal.json")
        status_service = CandidateStatusService(storage)  # Explicitly no automation.
        status = status_service.get(REQUEST)
        feedback = status_service._latest_validation_failure(REQUEST, claim["source_candidate"])
        manifests = [path for path in storage.list_candidate_paths(f"candidates/{REQUEST}/", 100) if path.endswith("/candidate-manifest.json")]
        approvals = storage.list_registry_paths(f"packages/{PLAN}/approvals/", 100)
        authority = _read(storage, f"author-budget/v1/groups/{GROUP}/recoveries/2.authorization.json")
        gate = _read(storage, f"packages/{PLAN}/coordination/writer.v1.json")
        print(json.dumps({"budget": state, "failedClaimVersion": claim["version"], "failedPhase": claim["phase"],
            "failedReservationHash": claim["reservation_hash"], "failedTerminalHash": _hash(terminal),
            "terminalState": terminal["state"], "terminalCandidate": terminal.get("candidate_id"),
            "sourceCandidate": claim["source_candidate"], "claimFailureHash": claim["failure_hash"],
            "feedbackHash": _hash(feedback), "repairable": feedback.get("repairable"),
            "candidateStatus": {key: status.get(key) for key in ("request_id", "candidate_id", "status", "automation_error")},
            "manifestPaths": manifests, "approvalCount": len(approvals), "recoveryAuthorityAbsent": authority is None,
            "writerGate": None if gate is None else {key: gate.get(key) for key in ("version", "plan_id", "state", "operation")}}, indent=2))
    finally:
        service.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(json.dumps({"code": "READ_ONLY_A022_PROBE_FAILED", "type": type(error).__name__}))
        raise SystemExit(1)