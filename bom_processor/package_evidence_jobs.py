"""Immutable evidence requests and injected job execution, no Azure/model defaults.

Inputs: approved checkpoint, pinned staged blob references and injected adapters.
Outputs: request/status/proposal artifacts; never approval or automatic acceptance.
Run: python -m pytest tests/test_package_evidence_jobs.py.
"""
from __future__ import annotations

import json
from typing import Any

from .core import BlobConflictError, BlobNotFoundError
from .package_evidence_contracts import (
    PackageEvidenceError, assemblies_metadata, detached, digest, hash_value,
    integer, json_bytes, object_hash, require, strict, text, uuid_value,
)
from .package_evidence_proposal import produce_proposal

VERSION = "package-evidence-request.v1"
MAX_METADATA = 2_000_000
MAX_GRAPH = 8 * 1024 * 1024
CHECKPOINT = {"qqUuid", "packageVersion", "bomPlanId", "bomApprovalId", "resolvedManifestSha256"}


def evidence_request(context: dict, roots: list[dict], workbooks: list[dict], policy: dict) -> dict:
    """Build an initial-only request. Authenticated server owns context/policy.

    References use an exact package/document/hash prefix, never URLs or SAS.
    This validates reference structure, not authority to access a customer's bytes.
    HTTP callers must authenticate and verify checkpoint ownership separately.
    """
    strict(context, CHECKPOINT | {"assemblies"})
    uuid_value(context["qqUuid"])
    integer(context["packageVersion"])
    for key in CHECKPOINT - {"qqUuid", "packageVersion"}:
        hash_value(context[key])
    context = {**detached(context), "assemblies": assemblies_metadata(context["assemblies"])}
    require(all(a["canonicalBom"] is not None for a in context["assemblies"]), "BOM_REQUIRED")
    strict(policy, {"producer", "model", "policyVersion", "promptSha256"})
    for key in ("producer", "model", "policyVersion"):
        text(policy[key])
    hash_value(policy["promptSha256"])
    prefix = f'evidence-inputs/{context["qqUuid"]}/package-{context["packageVersion"]}/'
    locations: dict[str, dict] = {}

    def references(values: list, *, files: bool) -> list:
        require(type(values) is list and 1 <= len(values) <= 10000, "INVALID_REFERENCES")
        result = []
        seen = set()
        for item in values:
            strict(item, {"documentId", "sha256", "sizeBytes", "blobPath"} | ({"fileName"} if files else set()))
            identity = uuid_value(item["documentId"])
            require(identity not in seen, "DUPLICATE_REFERENCE")
            seen.add(identity)
            sha = hash_value(item["sha256"])
            integer(item["sizeBytes"], 0, 50_000_000)
            require(item["blobPath"] == f"{prefix}{identity}/{sha}", "INVALID_BLOB_REFERENCE")
            if files:
                name = text(item["fileName"])
                require(not any(c in name for c in ("/", "\\", ":")), "INVALID_FILENAME")
            metadata = {k: item[k] for k in ("documentId", "sha256", "sizeBytes", "blobPath")}
            require(locations.setdefault(identity, metadata) == metadata, "CONFLICTING_REFERENCE")
            result.append(detached(item))
        return sorted(result, key=lambda item: item["documentId"])

    roots = references(roots, files=True)
    workbooks = references(workbooks, files=False)
    expected = {a["canonicalBom"]["documentId"]: a["canonicalBom"] for a in context["assemblies"]}
    require({w["documentId"] for w in workbooks} == set(expected), "WORKBOOK_COVERAGE")
    for book in workbooks:
        require({k: book[k] for k in ("documentId", "sha256", "sizeBytes")} == expected[book["documentId"]], "WORKBOOK_BINDING")
    require(sum(r["sizeBytes"] for r in locations.values()) <= 200_000_000, "REQUEST_BYTE_LIMIT")
    body = {"version": VERSION, "context": context, "roots": roots, "workbooks": workbooks, "policy": detached(policy)}
    require(len(json_bytes(body)) <= MAX_METADATA, "REQUEST_METADATA_LIMIT")
    return {**body, "requestId": object_hash(body)}


class EvidenceJobService:
    """Storage must implement atomic immutable create/verify and typed not-found.

    Injected starter.start receives only REQUEST_ID. No retries/claim stealing;
    ambiguous starts and interrupted executions require explicit reconciliation.
    Status is read-only. run is the worker seam; deployed wiring remains separate.
    """

    def __init__(self, storage: Any, starter: Any = None):
        self.storage, self.starter = storage, starter

    @staticmethod
    def _path(request_id: str, artifact: str) -> str:
        return f"package-evidence/{hash_value(request_id)}/{artifact}.json"

    def _write(self, request_id: str, artifact: str, value: dict) -> None:
        self.storage.upload_registry_immutable(self._path(request_id, artifact), json_bytes(value), "application/json")

    def _read(self, request_id: str, artifact: str, limit: int = MAX_METADATA) -> dict | None:
        try:
            raw = self.storage.download_registry(self._path(request_id, artifact), limit)
        except BlobNotFoundError:
            return None
        try:
            def pairs(items):
                value = {}
                for key, item in items:
                    require(key not in value, "ARTIFACT_CORRUPT")
                    value[key] = item
                return value
            value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                               parse_constant=lambda _value: (_ for _ in ()).throw(PackageEvidenceError("ARTIFACT_CORRUPT")))
            require(type(value) is dict and json_bytes(value) == raw, "ARTIFACT_CORRUPT")
            return value
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise PackageEvidenceError("ARTIFACT_CORRUPT") from exc

    def request(self, request_id: str) -> dict:
        value = self._read(request_id, "request")
        require(value is not None, "REQUEST_NOT_FOUND")
        strict(value, {"version", "context", "roots", "workbooks", "policy", "requestId"})
        rebuilt = evidence_request(value["context"], value["roots"], value["workbooks"], value["policy"])
        require(value == rebuilt and value["requestId"] == request_id, "REQUEST_CORRUPT")
        return value

    def submit(self, supplied: dict) -> dict:
        strict(supplied, {"version", "context", "roots", "workbooks", "policy", "requestId"})
        request = evidence_request(supplied["context"], supplied["roots"], supplied["workbooks"], supplied["policy"])
        require(supplied == request, "REQUEST_CORRUPT")
        self._write(request["requestId"], "request", request)
        return self.status(request["requestId"])

    def start(self, request_id: str) -> dict:
        self.request(request_id)
        existing = self.status(request_id)
        if existing["status"] != "requested":
            return existing
        require(self.starter is not None, "STARTER_UNAVAILABLE")
        # A unique claim nonce prevents create-or-verify from granting two callers
        # ownership when both upload the same deterministic marker bytes.
        import uuid
        claim = {"requestId": request_id, "claimId": uuid.uuid4().hex}
        try:
            self._write(request_id, "start-claim", claim)
        except BlobConflictError:
            return self.status(request_id)
        try:
            execution = self.starter.start({"REQUEST_ID": request_id})
            text(execution, 256)
            self._write(request_id, "started", {"requestId": request_id, "execution": execution})
        except Exception:
            # Transport exception could mean Azure accepted the job. Never retry.
            self._write(request_id, "start-unknown", {"requestId": request_id, "code": "START_UNKNOWN"})
        return self.status(request_id)

    def status(self, request_id: str) -> dict:
        request = self.request(request_id)
        completed = self._read(request_id, "completed")
        if completed is not None:
            classified = request["policy"]["policyVersion"] == "package-evidence-foundry.v2"
            strict(completed, {"requestId", "proposalSha256", "artifactSha256"}
                   | ({"classificationArtifactSha256"} if classified else set()))
            graph = self._read(request_id, "proposal", MAX_GRAPH)
            require(graph is not None and completed["requestId"] == request_id
                    and digest(json_bytes(graph)) == completed["artifactSha256"], "RESULT_CORRUPT")
            require(graph.get("proposalSha256") == completed["proposalSha256"]
                    and object_hash({k: v for k, v in graph.items() if k != "proposalSha256"}) == graph["proposalSha256"], "RESULT_CORRUPT")
            require(all(graph.get(k) == request["context"][k] for k in CHECKPOINT)
                    and graph.get("assemblies") == request["context"]["assemblies"]
                    and graph.get("parentProposalSha256") is None
                    and graph.get("provenance") == {"kind": "agent", **request["policy"], "runId": request_id}, "RESULT_BINDING")
            result = {"requestId": request_id, "status": "completed", "proposal": graph}
            if classified:
                classification = self._classification(request_id, graph)
                require(digest(json_bytes(classification)) == completed["classificationArtifactSha256"],
                        "CLASSIFICATION_RESULT_CORRUPT")
                result["classification"] = classification
            return result
        for artifact, state in (("failed", "failed"), ("worker-claim", "running"),
                                ("start-unknown", "start_unknown"), ("started", "started"),
                                ("start-claim", "start_unknown")):
            marker = self._read(request_id, artifact)
            if marker is not None:
                fields = {"requestId", "claimId"} if artifact.endswith("claim") else {"requestId", "execution"} if artifact == "started" else {"requestId", "code"}
                strict(marker, fields, "MARKER_CORRUPT")
                require(marker.get("requestId") == request_id, "MARKER_CORRUPT")
                if "claimId" in marker:
                    import re
                    require(type(marker["claimId"]) is str and re.fullmatch(r"[0-9a-f]{32}", marker["claimId"]) is not None, "MARKER_CORRUPT")
                if "execution" in marker:
                    text(marker["execution"])
                if "code" in marker:
                    require(marker["code"] == ("EVIDENCE_JOB_FAILED" if artifact == "failed" else "START_UNKNOWN"), "MARKER_CORRUPT")
                return {"requestId": request_id, "status": state}
        return {"requestId": request_id, "status": "requested"}

    def reconcile_publication(self, request_id: str) -> dict:
        """Explicit recovery only: publish existing pinned result, never invoke model.

        No worker/start claims are removed or stolen. Consumer independently
        verifies graph semantics before import; here hash/checkpoint/provenance
        bind the already-written artifact to this exact request.
        """
        request = self.request(request_id)
        graph = self._read(request_id, "proposal", MAX_GRAPH)
        require(graph is not None, "PROPOSAL_NOT_FOUND")
        require(graph.get("version") == "package-evidence-graph.v1"
                and object_hash({k: v for k, v in graph.items() if k != "proposalSha256"}) == graph.get("proposalSha256"), "RESULT_CORRUPT")
        require(all(graph.get(k) == request["context"][k] for k in CHECKPOINT)
                and graph.get("assemblies") == request["context"]["assemblies"]
                and graph.get("parentProposalSha256") is None
                and graph.get("provenance") == {"kind": "agent", **request["policy"], "runId": request_id}, "RESULT_BINDING")
        completed = {"requestId": request_id, "proposalSha256": graph["proposalSha256"],
                     "artifactSha256": digest(json_bytes(graph))}
        if request["policy"]["policyVersion"] == "package-evidence-foundry.v2":
            classification = self._classification(request_id, graph)
            completed["classificationArtifactSha256"] = digest(json_bytes(classification))
        self._write(request_id, "completed", completed)
        return self.status(request_id)

    def _classification(self, request_id: str, graph: dict) -> dict:
        """Publication integrity only; consumer independently rereads workbook rows."""
        value = self._read(request_id, "classification", MAX_GRAPH)
        require(value is not None, "CLASSIFICATION_RESULT_MISSING")
        strict(value, {"version", "policyVersion", "proposalSha256", "rowObservationsSha256",
                       "producer", "model", "promptSha256", "runId", "classifications", "classificationSha256"})
        require(value["version"] == "package-part-classification.v1"
                and value["policyVersion"] == "agent-inferred-part-type.v1"
                and object_hash({k: v for k, v in value.items() if k != "classificationSha256"})
                    == value["classificationSha256"]
                and value["proposalSha256"] == graph["proposalSha256"]
                and all(value[k] == graph["provenance"][k]
                        for k in ("producer", "model", "promptSha256", "runId")),
                "CLASSIFICATION_RESULT_BINDING")
        return value

    def run(self, request_id: str, model: Any) -> dict:
        request = self.request(request_id)
        existing = self.status(request_id)
        if existing["status"] in {"completed", "failed", "running"}:
            return existing
        # Never execute a historical request under a newer adapter/prompt while
        # recording its old attribution. This check precedes the worker claim.
        for attribute, key in (("policy_version", "policyVersion"), ("prompt_sha256", "promptSha256")):
            if hasattr(model, attribute):
                require(getattr(model, attribute) == request["policy"][key], "MODEL_POLICY_BINDING")
        import uuid
        try:
            self._write(request_id, "worker-claim", {"requestId": request_id, "claimId": uuid.uuid4().hex})
        except BlobConflictError:
            return self.status(request_id)
        try:
            cache = {}
            for item in request["roots"] + request["workbooks"]:
                if item["documentId"] in cache:
                    continue
                data = self.storage.download_source(item["blobPath"], item["sizeBytes"])
                require(type(data) is bytes and len(data) == item["sizeBytes"]
                        and digest(data) == item["sha256"], "SOURCE_IDENTITY_MISMATCH")
                cache[item["documentId"]] = data
            roots = [{k: r[k] for k in ("documentId", "sha256", "sizeBytes", "fileName")} | {"data": cache[r["documentId"]]} for r in request["roots"]]
            policy = request["policy"]
            classified = policy["policyVersion"] == "package-evidence-foundry.v2"
            produced = produce_proposal(request["context"], roots,
                {w["documentId"]: cache[w["documentId"]] for w in request["workbooks"]}, model=model,
                producer=policy["producer"], model_name=policy["model"], policy_version=policy["policyVersion"],
                prompt_sha256=policy["promptSha256"], run_id=request_id,
                include_classification=classified)
            graph = produced["proposal"] if classified else produced
        except Exception as exc:
            self._write(request_id, "failed", {"requestId": request_id, "code": "EVIDENCE_JOB_FAILED"})
            raise PackageEvidenceError("EVIDENCE_JOB_FAILED") from exc
        # Publication uncertainty is not execution failure. Retain the claim;
        # explicit reconciliation can complete an existing proposal without billing
        # another model run. A missing proposal cannot be fabricated/reexecuted.
        try:
            self._write(request_id, "proposal", graph)
            if classified:
                self._write(request_id, "classification", produced["classification"])
            return self.reconcile_publication(request_id)
        except Exception as exc:
            raise PackageEvidenceError("PUBLICATION_UNKNOWN") from exc