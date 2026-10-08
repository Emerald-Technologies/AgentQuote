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
    ASSESSED_POLICIES, CLASSIFIED_POLICIES, PackageEvidenceError, RETAINED_POLICIES, REVIEWED_POLICIES,
    assemblies_metadata, detached, digest, failure_causes, hash_value, integer, json_bytes, object_hash, require,
    strict, text, uuid_value,
)
from .package_evidence_inventory import AGGREGATE_BYTES_LIMIT, ROOT_BYTES_LIMIT
from .package_evidence_proposal import produce_proposal

VERSION = "package-evidence-request.v1"
MAX_METADATA = 2_000_000
MAX_GRAPH = 8 * 1024 * 1024
# Failure diagnostics (operator evidence only; never read back by status, never logged).
DIAGNOSTIC_VERSION = "package-evidence-failure-diagnostic.v1"
MAX_DIAGNOSTIC_REPLY_BYTES = 256 * 1024
CHECKPOINT = {"qqUuid", "packageVersion", "bomPlanId", "bomApprovalId", "resolvedManifestSha256"}


V3 = "package-evidence-request.v3"
V4 = "package-evidence-request.v4"  # operator retry of a failed request (package_evidence_retry)


def request_fields(value: Any) -> set[str]:
    version = value.get("version") if type(value) is dict else None
    return ({"version", "context", "roots", "workbooks", "policy", "requestId"}
            | ({"retainedBomSources"} if version in {"package-evidence-request.v2", V3, V4} else set())
            | ({"successor"} if version == V3 else set())
            | ({"retry"} if version == V4 else set()))


def expected_parent(request: dict) -> str | None:
    successor = request.get("successor")
    return None if successor is None else successor["predecessorProposalSha256"]


def evidence_request(context: dict, roots: list[dict], workbooks: list[dict], policy: dict,
                     retained_bom_sources: dict | None = None, successor: dict | None = None,
                     retry: dict | None = None) -> dict:
    """Build an initial, bounded-successor or failed-request retry. Server owns context/policy/feedback.

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
            integer(item["sizeBytes"], 0, ROOT_BYTES_LIMIT)
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
    require(sum(r["sizeBytes"] for r in locations.values()) <= AGGREGATE_BYTES_LIMIT, "REQUEST_BYTE_LIMIT")
    body = {"version": VERSION, "context": context, "roots": roots, "workbooks": workbooks, "policy": detached(policy)}
    require((policy["policyVersion"] in RETAINED_POLICIES) == (retained_bom_sources is not None), "RETENTION_POLICY_BINDING")
    if retained_bom_sources is not None:
        from .package_evidence_retention import validate_retained_sources
        validate_retained_sources(retained_bom_sources, roots)
        body.update(version="package-evidence-request.v2", retainedBomSources=detached(retained_bom_sources))
    if successor is not None:
        # Successors exist only for the retained-provenance policy actually deployed.
        require(retained_bom_sources is not None, "SUCCESSOR_POLICY_BINDING")
        from .package_evidence_successor import validate_successor
        body.update(version=V3, successor=validate_successor(successor))
    if retry is not None:
        # A retry replaces a failed request that has no proposal; it never also carries
        # qualification feedback, and only the retained-provenance policy is deployed.
        require(successor is None and retained_bom_sources is not None, "RETRY_POLICY_BINDING")
        from .package_evidence_retry import validate_retry
        body.update(version=V4, retry=validate_retry(retry))
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
        strict(value, request_fields(value))
        rebuilt = evidence_request(value["context"], value["roots"], value["workbooks"], value["policy"],
                                   value.get("retainedBomSources"), value.get("successor"), value.get("retry"))
        require(value == rebuilt and value["requestId"] == request_id, "REQUEST_CORRUPT")
        return value

    def _require_retry_lineage(self, request: dict) -> None:
        """A retry is valid only for an existing FAILED initial/retry request with identical
        package inputs and the immediately previous attempt number."""
        from .package_evidence_retry import BOUND_FIELDS, request_attempt
        binding = request["retry"]
        prior_id = binding["retryOfRequestId"]
        require(prior_id != request["requestId"], "RETRY_LINEAGE_INVALID")
        try:
            prior = self.request(prior_id)
        except PackageEvidenceError as exc:
            raise PackageEvidenceError("RETRY_PREDECESSOR_NOT_FOUND") from exc
        require(prior["version"] in {"package-evidence-request.v2", V4}, "RETRY_PREDECESSOR_UNSUPPORTED")
        require(self.status(prior_id)["status"] == "failed", "RETRY_PREDECESSOR_NOT_FAILED")
        require(all(prior.get(key) == request.get(key) for key in BOUND_FIELDS), "RETRY_INPUT_MISMATCH")
        require(binding["attempt"] == request_attempt(prior) + 1, "RETRY_LINEAGE_INVALID")

    def submit(self, supplied: dict) -> dict:
        strict(supplied, request_fields(supplied))
        request = evidence_request(supplied["context"], supplied["roots"], supplied["workbooks"], supplied["policy"],
                                   supplied.get("retainedBomSources"), supplied.get("successor"), supplied.get("retry"))
        require(supplied == request, "REQUEST_CORRUPT")
        if "retry" in request:
            self._require_retry_lineage(request)
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
            classified = request["policy"]["policyVersion"] in CLASSIFIED_POLICIES
            assessed = request["policy"]["policyVersion"] in ASSESSED_POLICIES
            reviewed = request["policy"]["policyVersion"] in REVIEWED_POLICIES
            strict(completed, {"requestId", "proposalSha256", "artifactSha256"}
                     | ({"classificationArtifactSha256"} if classified else set())
                     | ({"assessmentArtifactSha256"} if assessed else set())
                     | ({"reviewArtifactSha256"} if reviewed else set()))
            graph = self._read(request_id, "proposal", MAX_GRAPH)
            require(graph is not None and completed["requestId"] == request_id
                    and digest(json_bytes(graph)) == completed["artifactSha256"], "RESULT_CORRUPT")
            require(graph.get("proposalSha256") == completed["proposalSha256"]
                    and object_hash({k: v for k, v in graph.items() if k != "proposalSha256"}) == graph["proposalSha256"], "RESULT_CORRUPT")
            require(all(graph.get(k) == request["context"][k] for k in CHECKPOINT)
                    and graph.get("assemblies") == request["context"]["assemblies"]
                    and graph.get("parentProposalSha256") == expected_parent(request)
                    and graph.get("provenance") == {"kind": "agent", **request["policy"], "runId": request_id}, "RESULT_BINDING")
            result = {"requestId": request_id, "status": "completed", "proposal": graph}
            if classified:
                classification = self._classification(request_id, graph)
                require(digest(json_bytes(classification)) == completed["classificationArtifactSha256"],
                        "CLASSIFICATION_RESULT_CORRUPT")
                result["classification"] = classification
            if assessed:
                assessment = self._assessment(request_id, graph, classification)
                require(digest(json_bytes(assessment)) == completed["assessmentArtifactSha256"], "ASSESSMENT_RESULT_CORRUPT")
                result["manufacturingAssessment"] = assessment
            if reviewed:
                review = self._review(request_id, graph, classification, assessment)
                require(digest(json_bytes(review)) == completed["reviewArtifactSha256"], "REVIEW_RESULT_CORRUPT")
                result["review"] = review
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
                and graph.get("parentProposalSha256") == expected_parent(request)
                and graph.get("provenance") == {"kind": "agent", **request["policy"], "runId": request_id}, "RESULT_BINDING")
        completed = {"requestId": request_id, "proposalSha256": graph["proposalSha256"],
                     "artifactSha256": digest(json_bytes(graph))}
        if request["policy"]["policyVersion"] in CLASSIFIED_POLICIES:
            classification = self._classification(request_id, graph)
            completed["classificationArtifactSha256"] = digest(json_bytes(classification))
        if request["policy"]["policyVersion"] in ASSESSED_POLICIES:
            assessment = self._assessment(request_id, graph, classification)
            completed["assessmentArtifactSha256"] = digest(json_bytes(assessment))
        if request["policy"]["policyVersion"] in REVIEWED_POLICIES:
            review = self._review(request_id, graph, classification, assessment)
            completed["reviewArtifactSha256"] = digest(json_bytes(review))
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

    def _review(self, request_id: str, graph: dict, classification: dict, assessment: dict) -> dict:
        from .package_evidence_review import validate_review_artifact
        value = self._read(request_id, "review", MAX_GRAPH)
        require(value is not None, "REVIEW_RESULT_MISSING")
        return validate_review_artifact(value, graph, classification, assessment)

    def _assessment(self, request_id: str, graph: dict, classification: dict) -> dict:
        value = self._read(request_id, "manufacturing-assessment", MAX_GRAPH)
        require(value is not None, "ASSESSMENT_RESULT_MISSING")
        strict(value, {"version", "policyVersion", "proposalSha256", "classificationSha256", "contentSha256",
                       "producer", "model", "promptSha256", "runId", "requirements", "relationships", "assessmentSha256"})
        require(value["version"] == "package-manufacturing-assessment.v1"
                and value["policyVersion"] == "source-grounded-manufacturing.v1"
                and object_hash({k: v for k, v in value.items() if k != "assessmentSha256"}) == value["assessmentSha256"]
                and value["proposalSha256"] == graph["proposalSha256"]
                and value["classificationSha256"] == classification["classificationSha256"]
                and all(value[k] == graph["provenance"][k] for k in ("producer", "model", "promptSha256", "runId")), "ASSESSMENT_RESULT_BINDING")
        return value

    def _write_failure_diagnostic(self, request_id: str, exc: BaseException, model: Any) -> None:
        """Best effort: keep the rejected producer reply next to the request so the exact
        validation failure can be inspected. Same protected container as proposal.json; the
        failed marker is already written, so any error here never changes the job outcome."""
        try:
            reply = getattr(model, "last_producer_reply", None)
            record: dict | None = None
            if isinstance(reply, str):
                raw = reply.encode("utf-8", errors="replace")
                record = {"sha256": digest(raw), "sizeBytes": len(raw),
                          "text": reply if len(raw) <= MAX_DIAGNOSTIC_REPLY_BYTES else None,
                          "omitted": None if len(raw) <= MAX_DIAGNOSTIC_REPLY_BYTES else "REPLY_TOO_LARGE"}
            self._write(request_id, "failure-diagnostic", {
                "version": DIAGNOSTIC_VERSION, "requestId": request_id,
                "causes": failure_causes(exc), "producerReply": record})
        except Exception:
            pass

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
        require(request["policy"]["policyVersion"] not in REVIEWED_POLICIES
                or (callable(getattr(model, "review", None)) and hasattr(model, "reviewer_identity")), "REVIEWER_REQUIRED")
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
            classified = policy["policyVersion"] in CLASSIFIED_POLICIES
            assessed = policy["policyVersion"] in ASSESSED_POLICIES
            reviewed = policy["policyVersion"] in REVIEWED_POLICIES
            review_inputs: dict | None = {} if reviewed else None
            successor = request.get("successor")
            context = request["context"] if successor is None else {
                **request["context"], "predecessorProposalSha256": successor["predecessorProposalSha256"]}
            produced = produce_proposal(context, roots,
                {w["documentId"]: cache[w["documentId"]] for w in request["workbooks"]}, model=model,
                producer=policy["producer"], model_name=policy["model"], policy_version=policy["policyVersion"],
                prompt_sha256=policy["promptSha256"], run_id=request_id,
                include_classification=classified, include_manufacturing=assessed,
                retained_bom_sources=request.get("retainedBomSources"),
                correction=None if successor is None else {"attempt": successor["attempt"], "feedback": successor["feedback"]},
                review_inputs=review_inputs)
            graph = produced["proposal"] if classified else produced
            review = None
            if reviewed:
                # Second, independent model call. Its failure never discards the paid proposal:
                # a failed review artifact is published and QuickQuote routes the package to a human.
                from .package_evidence_review import review_artifact, review_packet
                packet = review_packet(graph, produced["classification"], produced["manufacturingAssessment"],
                                       review_inputs["sources"], review_inputs["inventory"], review_inputs["rows"],
                                       review_inputs["retained"])
                hints, error = None, None
                try:
                    hints = model.review(packet)
                except PackageEvidenceError as exc:
                    error = str(exc)
                except Exception:
                    error = "REVIEW_REQUEST_FAILED"
                review = review_artifact(graph, produced["classification"], produced["manufacturingAssessment"],
                                         packet, hints, error, model.reviewer_identity)
        except Exception as exc:
            self._write(request_id, "failed", {"requestId": request_id, "code": "EVIDENCE_JOB_FAILED"})
            self._write_failure_diagnostic(request_id, exc, model)
            raise PackageEvidenceError("EVIDENCE_JOB_FAILED") from exc
        # Publication uncertainty is not execution failure. Retain the claim;
        # explicit reconciliation can complete an existing proposal without billing
        # another model run. A missing proposal cannot be fabricated/reexecuted.
        try:
            self._write(request_id, "proposal", graph)
            if classified:
                self._write(request_id, "classification", produced["classification"])
            if assessed:
                self._write(request_id, "manufacturing-assessment", produced["manufacturingAssessment"])
            if reviewed:
                self._write(request_id, "review", review)
            return self.reconcile_publication(request_id)
        except Exception as exc:
            raise PackageEvidenceError("PUBLICATION_UNKNOWN") from exc