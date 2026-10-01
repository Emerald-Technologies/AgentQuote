"""Byte-bound parent package planning, review, approval, and publication."""
from __future__ import annotations

import hashlib
import io
import json
import re
from datetime import datetime, timezone
from typing import Any, Callable

from .candidate_status import CandidateStatusError, CandidateStatusService
from .core import (CANONICAL_COLUMNS, OUTPUT_CONTENT_TYPE, BlobConflictError,
                   BlobNotFoundError, ProcessingError, ProcessingRequest, _json_bytes)
from .semantic import AssemblyIdentity, reconcile_boms
from .semantic_facts import SemanticFactsError, facts_from_xlsx

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_MAX_METADATA = 1_000_000
_RECONCILIATIONS = frozenset({"equivalent", "merged", "human_selected_conflict"})
_ZIP_EPOCH = (2000, 1, 1, 0, 0, 0)
_WORKBOOK_EPOCH = datetime(2000, 1, 1)


class PackageResolutionError(ProcessingError):
    """Fail-closed parent package operation."""


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _ids(*values: str) -> None:
    if not all(isinstance(value, str) and _ID.fullmatch(value) for value in values):
        raise PackageResolutionError("INVALID_PACKAGE_ID")


def _json(storage: Any, path: str, *, candidate: bool = False,
          output: bool = False) -> dict[str, Any]:
    try:
        if candidate:
            raw = storage.download_candidate(path, _MAX_METADATA)
        elif output:
            raw = storage.download_output(path, _MAX_METADATA)
            if raw is None:
                raise BlobNotFoundError("artifact unavailable")
        else:
            raw = storage.download_registry(path, _MAX_METADATA)
        value = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise PackageResolutionError("PACKAGE_ARTIFACT_INVALID") from exc
    if not isinstance(value, dict):
        raise PackageResolutionError("PACKAGE_ARTIFACT_INVALID")
    return value


class ParentPackageService:
    """Project a processing request and its child workflows into one package."""

    def __init__(self, storage: Any, *, clock: Callable[[], datetime] | None = None,
                 max_source_bytes: int = 50_000_000,
                 max_output_bytes: int = 50_000_000,
                 max_parser_bytes: int = 1_000_000, max_rows: int = 100_000):
        self.storage = storage
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.max_source_bytes = max_source_bytes
        self.max_output_bytes = max_output_bytes
        self.max_parser_bytes = max_parser_bytes
        self.max_rows = max_rows

    def plan(self, request: ProcessingRequest, *,
             approved_assignments: list[dict[str, Any]],
             pending_assignments: list[dict[str, Any]]) -> dict[str, Any]:
        """Persist an immutable inventory after independently verifying every source."""
        request.validate()
        sources = self._verified_sources(request)
        processed_prefix = self._request_processed_prefix(request)
        assignments: list[dict[str, Any]] = []
        covered: list[str] = []
        for supplied in approved_assignments:
            document_ids = self._documents(supplied, sources)
            manifest_path = supplied.get("manifest_path")
            if manifest_path is None:
                assignments.append(self._approved_selection(request, document_ids, supplied))
            else:
                if (not isinstance(manifest_path, str) or not manifest_path.startswith(processed_prefix)
                        or not manifest_path.endswith("/manifest.json") or "\\" in manifest_path
                        or ".." in manifest_path.split("/")):
                    raise PackageResolutionError("APPROVED_ASSIGNMENT_INVALID")
                manifest = _json(self.storage, manifest_path, output=True)
                assignments.append(self._approved_assignment(
                    request, document_ids, manifest_path, manifest, processed_prefix))
            covered.extend(document_ids)
        for supplied in pending_assignments:
            document_ids = self._documents(supplied, sources)
            request_id = supplied.get("request_id")
            _ids(request_id)
            child = _json(self.storage, f"candidates/requests/{request_id}.json")
            child_sources = self._normalized_child_sources(child.get("source_evidence"))
            if (child.get("request_id") != request_id or child.get("parser_key") != request.parser_key
                    or child.get("customer_key") != request.customer_key
                    or child_sources != [sources[item] for item in document_ids]):
                raise PackageResolutionError("CANDIDATE_ASSIGNMENT_INVALID")
            assignments.append({"kind": "candidate_request", "document_ids": document_ids,
                                "request_id": request_id, "profile_hash": child.get("profile_hash")})
            covered.extend(document_ids)
        if sorted(covered) != sorted(sources) or len(covered) != len(set(covered)):
            raise PackageResolutionError("PACKAGE_COVERAGE_INVALID")
        assignments.sort(key=lambda item: (item["document_ids"], item["kind"],
                                           item.get("request_id", ""), item.get("manifest_path", "")))
        identity = {"version": "package-plan.v1", "request_fingerprint": request.fingerprint(),
                    "job_id": request.job_id, "customer_key": request.customer_key,
                    "parser_key": request.parser_key, "rfq_uuid": request.rfq_uuid.lower(),
                    "package_version": request.package_version,
                    "sources": [sources[key] for key in sorted(sources)], "assignments": assignments}
        plan_id = _hash(identity)
        plan = {**identity, "plan_id": plan_id}
        path = self._plan_path(plan_id)
        try:
            self.storage.upload_registry_immutable(path, _json_bytes(plan), "application/json")
        except BlobConflictError as exc:
            if _json(self.storage, path) != plan:
                raise PackageResolutionError("PACKAGE_PLAN_CONFLICT") from exc
        return plan

    def review(self, plan_id: str, *, assembly_mappings: dict[str, dict[str, str]] | None = None,
               conflict_dispositions: dict[str, str] | None = None) -> dict[str, Any]:
        _ids(plan_id)
        plan = self._plan(plan_id)
        mappings = self._mappings(assembly_mappings or {})
        dispositions = self._dispositions(conflict_dispositions or {})
        groups: list[dict[str, Any]] = []
        complete = True
        for assignment in plan["assignments"]:
            if assignment["kind"] == "approved_manifest":
                try:
                    groups.extend(self._approved_outputs(plan, assignment, mappings))
                except PackageResolutionError:
                    complete = False
                    groups.append({"kind": assignment["kind"], "document_ids": assignment["document_ids"],
                                   "status": "blocked", "mapping_keys": [], "disposition_keys": []})
                continue
            if assignment["kind"] == "approved_selection":
                try:
                    groups.extend(self._approved_selection_outputs(plan, assignment, mappings))
                except PackageResolutionError:
                    complete = False
                    groups.append({"kind": assignment["kind"], "document_ids": assignment["document_ids"],
                                   "status": "pending", "mapping_keys": [], "disposition_keys": []})
                continue
            try:
                child = CandidateStatusService(self.storage).get(assignment["request_id"])
            except (CandidateStatusError, BlobNotFoundError, ProcessingError) as exc:
                raise PackageResolutionError("PACKAGE_REVIEW_INVALID") from exc
            projected: dict[str, Any] = {"kind": assignment["kind"],
                "document_ids": assignment["document_ids"], "request_id": assignment["request_id"],
                "child_status": child.get("status"), "candidate_id": child.get("candidate_id")}
            if child.get("review_error") == "review_semantic_binding":
                raise PackageResolutionError("PACKAGE_SEMANTIC_BINDING")
            if child.get("status") not in {"awaiting_approval", "approved"} or not isinstance(child.get("review"), dict):
                complete = False
                projected.update({"status": "pending", "mapping_keys": [], "disposition_keys": []})
            else:
                candidate_groups = self._candidate_groups(assignment, child["review"], mappings)
                groups.extend([{**projected, **item} for item in candidate_groups])
                complete &= all(item["status"] == "ready" for item in candidate_groups)
                continue
            groups.append(projected)
        groups = self._reconcile_review_groups(groups, dispositions)
        complete &= all(group.get("status") == "ready" for group in groups)
        used_mappings = {key for group in groups for key in group.get("mapping_keys", [])}
        used_dispositions = {key for group in groups for key in group.get("disposition_keys", [])}
        if used_mappings != set(mappings) or used_dispositions != set(dispositions):
            raise PackageResolutionError("PACKAGE_REVIEW_INPUT_INVALID")
        body = {"version": "package-review.v1", "plan_id": plan_id,
                "plan_sha256": _hash({key: value for key, value in plan.items() if key != "plan_id"}),
                "complete": complete, "approval_ready": complete,
                "status": "awaiting_approval" if complete else "pending",
                "source_count": len(plan["sources"]), "groups": groups,
                "assembly_mappings": mappings, "conflict_dispositions": dispositions}
        return {**body, "manifest_sha256": _hash(body)}

    def approve(self, *, plan_id: str, review_manifest_sha256: str,
                approver: str, assembly_mappings: dict[str, dict[str, str]] | None = None,
                conflict_dispositions: dict[str, str] | None = None,
                reason: str = "", ticket: str = "") -> dict[str, Any]:
        _ids(plan_id, approver)
        if (not _HASH.fullmatch(str(review_manifest_sha256)) or not isinstance(reason, str)
                or len(reason) > 2000 or not isinstance(ticket, str) or len(ticket) > 256):
            raise PackageResolutionError("PACKAGE_APPROVAL_INVALID")
        review = self.review(plan_id, assembly_mappings=assembly_mappings,
                             conflict_dispositions=conflict_dispositions)
        if not review["approval_ready"] or review["manifest_sha256"] != review_manifest_sha256:
            raise PackageResolutionError("PACKAGE_REVIEW_BINDING")
        identity = {"version": "package-approval.v1", "plan_id": plan_id,
                    "review_manifest_sha256": review_manifest_sha256, "review": review,
                    "approver": approver, "reason": reason.strip(), "ticket": ticket.strip()}
        approval_id = _hash(identity)
        now = self.clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        record = {**identity, "approval_id": approval_id,
                  "approved_time": now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")}
        path = self._approval_path(plan_id, approval_id)
        try:
            self.storage.upload_registry_immutable(path, _json_bytes(record), "application/json")
        except BlobConflictError as exc:
            existing = _json(self.storage, path)
            if ({key: existing.get(key) for key in identity} != identity
                    or existing.get("approval_id") != approval_id):
                raise PackageResolutionError("PACKAGE_APPROVAL_CONFLICT") from exc
            record = existing
        return record

    def resolve(self, *, plan_id: str, approval_id: str) -> dict[str, Any]:
        """Publish the resolved package; the completion marker is written last."""
        _ids(plan_id, approval_id)
        plan = self._plan(plan_id)
        approval = self._bound_approval(plan_id, approval_id)
        review = self.review(plan_id,
            assembly_mappings=approval.get("review", {}).get("assembly_mappings"),
            conflict_dispositions=approval.get("review", {}).get("conflict_dispositions"))
        if (not review["approval_ready"] or review["manifest_sha256"] != approval.get("review_manifest_sha256")
                or review != approval.get("review")):
            raise PackageResolutionError("PACKAGE_APPROVAL_BINDING")
        prefix = self._resolved_prefix(plan_id, approval_id)
        manifest_path = f"{prefix}/resolved-output-manifest.v1.json"
        existing = self.storage.download_output(manifest_path, self.max_output_bytes)
        if existing is not None:
            return self._validate_resolved(existing, plan, approval, prefix)
        processed_prefix = self._processed_prefix(plan)
        results = []
        for group in review["groups"]:
            evidence = group["evidence"]
            fixtures = evidence["fixtures"]
            row_sets = [self._rows(self._candidate_bytes(item["output"], item["prefix"])
                                   if item["storage"] == "candidate"
                                   else self._output_bytes(item["output"], processed_prefix))
                        for item in fixtures]
            assembly = evidence["assembly_identity"]
            reconciliation = reconcile_boms(row_sets, assembly=AssemblyIdentity(
                assembly["name"], assembly.get("revision", "")))
            disposition = evidence.get("conflict_disposition")
            if reconciliation.relation == "conflict":
                if disposition not in {item["fixture_key"] for item in fixtures}:
                    raise PackageResolutionError("PACKAGE_MATERIAL_CONFLICT")
                selected = next(item for item in fixtures if item["fixture_key"] == disposition)
                rows = self._rows(self._candidate_bytes(selected["output"], selected["prefix"])
                                  if selected["storage"] == "candidate"
                                  else self._output_bytes(selected["output"], processed_prefix))
                relation = "human_selected_conflict"
            else:
                rows, relation = list(reconciliation.rows), reconciliation.relation
            output = self._workbook(rows)
            digest = hashlib.sha256(output).hexdigest()
            output_path = f"{prefix}/objects/{digest}.xlsx"
            self.storage.upload_output_immutable(output_path, output, OUTPUT_CONTENT_TYPE)
            results.append({"group_id": group["group_id"],
                            "document_ids": group["document_ids"],
                            "assembly_identity": evidence["assembly_identity"],
                            "reconciliation": relation,
                            "evidence_sha256": _hash(evidence),
                            "provenance": self._result_provenance(evidence),
                            "output": {"blob_path": output_path, "sha256": digest,
                                       "size_bytes": len(output), "content_type": OUTPUT_CONTENT_TYPE}})
        results.sort(key=lambda item: item["document_ids"])
        manifest_identity = {"version": "resolved-output-manifest.v1", "status": "completed",
            "plan_id": plan_id, "approval_id": approval_id, "plan_sha256": review["plan_sha256"],
            "review_manifest_sha256": review["manifest_sha256"], "results": results}
        manifest = {**manifest_identity, "manifest_sha256": _hash(manifest_identity)}
        # The completion marker is intentionally last. Replays trust no partial set.
        self.storage.upload_output_immutable(manifest_path, _json_bytes(manifest), "application/json")
        return manifest

    def manifest(self, plan_id: str, approval_id: str) -> dict[str, Any]:
        """Return the published resolved-output manifest without side effects."""
        _ids(plan_id, approval_id)
        plan = self._plan(plan_id)
        approval = self._bound_approval(plan_id, approval_id)
        prefix = self._resolved_prefix(plan_id, approval_id)
        raw = self.storage.download_output(f"{prefix}/resolved-output-manifest.v1.json",
                                           self.max_output_bytes)
        if raw is None:
            raise PackageResolutionError("RESOLVED_OUTPUT_ABSENT")
        return self._validate_resolved(raw, plan, approval, prefix)

    def approval(self, plan_id: str) -> dict[str, Any]:
        """Recover the sole immutable approval for a plan without writing state."""
        _ids(plan_id)
        self._plan(plan_id)
        prefix = f"packages/{plan_id}/approvals/"
        try:
            # Read a bounded approval collection rather than asking the storage
            # adapter for only two entries. The adapter intentionally raises
            # when a prefix exceeds its limit; using the maximum supported
            # bound ensures ordinary multi-approval states (including 3+) are
            # classified as ambiguity rather than infrastructure failure.
            paths = self.storage.list_registry_paths(prefix, 100)
        except Exception as exc:
            raise PackageResolutionError("PACKAGE_APPROVAL_LOOKUP_FAILED") from exc
        if not paths:
            raise PackageResolutionError("PACKAGE_APPROVAL_ABSENT")
        if len(paths) > 1:
            raise PackageResolutionError("PACKAGE_APPROVAL_AMBIGUOUS")
        match = re.fullmatch(re.escape(prefix) + r"([0-9a-f]{64})\.json", paths[0])
        if match is None:
            raise PackageResolutionError("PACKAGE_APPROVAL_LOOKUP_INVALID")
        return self._bound_approval(plan_id, match.group(1))

    def resolved_workbook(self, plan_id: str, approval_id: str, group_id: str) -> bytes:
        """Return one byte-verified published workbook without writing state."""
        _ids(plan_id, approval_id, group_id)
        manifest = self.manifest(plan_id, approval_id)
        matches = [item for item in manifest["results"]
                   if isinstance(item, dict) and item.get("group_id") == group_id]
        if len(matches) != 1:
            raise PackageResolutionError("RESOLVED_OUTPUT_GROUP_INVALID")
        return self._output_bytes(matches[0].get("output"),
                                  f"{self._resolved_prefix(plan_id, approval_id)}/objects/")

    def resolution_status(self, plan_id: str, approval_id: str) -> dict[str, Any]:
        """Read existing approval/manifest state only; never triggers resolution."""
        _ids(plan_id, approval_id)
        self._plan(plan_id)
        approval: dict[str, Any] | None = None
        try:
            raw = self.storage.download_registry(self._approval_path(plan_id, approval_id), _MAX_METADATA)
        except BlobNotFoundError:
            raw = None
        if raw is not None:
            try:
                value = json.loads(raw.decode("utf-8"))
            except Exception as exc:
                raise PackageResolutionError("PACKAGE_ARTIFACT_INVALID") from exc
            if not isinstance(value, dict):
                raise PackageResolutionError("PACKAGE_ARTIFACT_INVALID")
            identity = {key: value.get(key) for key in (
                "version", "plan_id", "review_manifest_sha256", "review", "approver", "reason", "ticket")}
            if (value.get("version") != "package-approval.v1" or value.get("plan_id") != plan_id
                    or value.get("approval_id") != approval_id or _hash(identity) != approval_id):
                raise PackageResolutionError("PACKAGE_APPROVAL_BINDING")
            approval = value
        prefix = self._resolved_prefix(plan_id, approval_id)
        resolved = self.storage.download_output(
            f"{prefix}/resolved-output-manifest.v1.json", self.max_output_bytes) is not None
        return {"plan_id": plan_id, "approval_id": approval_id, "approval": approval,
                "resolved": resolved,
                "status": "resolved" if resolved else ("approved" if approval is not None else "unknown")}

    def _verified_sources(self, request: ProcessingRequest) -> dict[str, dict[str, Any]]:
        result = {}
        for document in request.documents:
            data = self.storage.download_source(document.blob_path, self.max_source_bytes)
            if len(data) != document.size_bytes or hashlib.sha256(data).hexdigest() != document.sha256.lower():
                raise PackageResolutionError("PACKAGE_SOURCE_INVALID")
            result[document.document_id] = {"document_id": document.document_id,
                "blob_path": document.blob_path, "size_bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest()}
        return result

    def _reconcile_review_groups(self, groups: list[dict[str, Any]],
                                 dispositions: dict[str, str]) -> list[dict[str, Any]]:
        """Merge all ready groups with the same assembly before approval."""
        passthrough = [item for item in groups if item.get("status") != "ready"]
        by_assembly: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for group in groups:
            if group.get("status") != "ready":
                continue
            identity = group["evidence"]["assembly_identity"]
            key = AssemblyIdentity(identity["name"], identity.get("revision", "")).normalized()
            by_assembly.setdefault(key, []).append(group)
        merged = []
        processed_prefix = None
        for identity, members in sorted(by_assembly.items()):
            fixtures = [fixture for member in members for fixture in member["evidence"]["fixtures"]]
            unique: dict[tuple[str, str], dict[str, Any]] = {}
            for fixture in fixtures:
                key = (fixture["storage"], fixture["output"]["blob_path"])
                if key in unique:
                    unique[key]["document_ids"] = sorted(set(unique[key]["document_ids"])
                                                          | set(fixture["document_ids"]))
                else:
                    unique[key] = dict(fixture)
            fixtures = sorted(unique.values(), key=lambda item: (item["fixture_key"], item["output"]["blob_path"]))
            row_sets = []
            for fixture in fixtures:
                if fixture["storage"] == "candidate":
                    data = self._candidate_bytes(fixture["output"], fixture["prefix"])
                else:
                    path = fixture["output"]["blob_path"]
                    processed_prefix = path.split("processed/", 1)[0] + "processed/"
                    data = self._output_bytes(fixture["output"], processed_prefix)
                row_sets.append(self._rows(data))
            reconciliation = reconcile_boms(row_sets, assembly=AssemblyIdentity(*identity))
            group_id = _hash({"assembly_identity": identity})
            disposition = dispositions.get(group_id)
            disposition_keys: list[str] = []
            if reconciliation.relation == "conflict":
                if disposition not in {item["fixture_key"] for item in fixtures}:
                    raise PackageResolutionError("PACKAGE_CONFLICT_DISPOSITION_REQUIRED")
                disposition_keys.append(group_id)
            elif disposition is not None:
                raise PackageResolutionError("PACKAGE_REVIEW_INPUT_INVALID")
            evidence = {"assembly_identity": {"name": identity[0], "revision": identity[1]},
                        "reconciliation": reconciliation.relation,
                        "reconciliation_evidence": list(reconciliation.evidence),
                        "fixtures": fixtures,
                        "sources": sorted({doc for item in fixtures for doc in item["document_ids"]}),
                        "children": [{key: member["evidence"].get(key) for key in (
                            "request_id", "candidate_id", "validation_id",
                            "review_manifest_sha256", "candidate", "manifest_path",
                            "manifest_sha256") if member["evidence"].get(key) is not None}
                            for member in members]}
            if disposition is not None:
                evidence["conflict_disposition"] = disposition
            candidate_ids = {member.get("candidate_id") for member in members
                             if member.get("candidate_id") is not None}
            merged.append({"group_id": group_id, "kind": "assembly",
                           "document_ids": evidence["sources"], "status": "ready",
                           "evidence": evidence,
                           **({"candidate_id": next(iter(candidate_ids))} if len(candidate_ids) == 1 else {}),
                           "mapping_keys": sorted({key for member in members for key in member.get("mapping_keys", [])}),
                           "disposition_keys": disposition_keys})
        return sorted(passthrough + merged, key=lambda item: (item.get("document_ids", []), item.get("kind", "")))

    @staticmethod
    def _result_provenance(evidence: dict[str, Any]) -> dict[str, Any]:
        return {"sources": evidence["sources"],
                "fixtures": [{key: item.get(key) for key in (
                    "fixture_key", "document_ids", "storage", "output_sha256",
                    "semantic_fingerprint", "model_summary", "source", "parser", "prefix")
                    if item.get(key) is not None} for item in evidence["fixtures"]]}

    @staticmethod
    def _documents(value: dict[str, Any], sources: dict[str, Any]) -> list[str]:
        items = value.get("document_ids") if isinstance(value, dict) else None
        if (not isinstance(items, list) or not items or any(not isinstance(item, str) for item in items)
                or len(items) != len(set(items)) or any(item not in sources for item in items)):
            raise PackageResolutionError("PACKAGE_COVERAGE_INVALID")
        return sorted(items)

    @staticmethod
    def _normalized_child_sources(value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            raise PackageResolutionError("CANDIDATE_ASSIGNMENT_INVALID")
        result = []
        for item in value:
            if not isinstance(item, dict):
                raise PackageResolutionError("CANDIDATE_ASSIGNMENT_INVALID")
            result.append({key: item.get(key) for key in ("document_id", "blob_path", "size_bytes", "sha256")})
        return sorted(result, key=lambda item: item["document_id"])

    @staticmethod
    def _request_processed_prefix(request: ProcessingRequest) -> str:
        return (f"{request.customer_key}/rfq-{request.rfq_uuid.lower()}/"
                f"package-{request.package_version}/processed/")

    @staticmethod
    def _processed_prefix(plan: dict[str, Any]) -> str:
        return (f"{plan['customer_key']}/rfq-{plan['rfq_uuid']}/"
                f"package-{plan['package_version']}/processed/")

    def _approved_assignment(self, request: ProcessingRequest, document_ids: list[str],
                             path: str, manifest: dict[str, Any],
                             processed_prefix: str) -> dict[str, Any]:
        if (manifest.get("status") != "completed" or manifest.get("request_fingerprint") != request.fingerprint()
                or manifest.get("job_id") != request.job_id or manifest.get("customer_key") != request.customer_key
                or manifest.get("rfq_uuid") != request.rfq_uuid.lower()
                or manifest.get("package_version") != request.package_version
                or manifest.get("parser", {}).get("selection_mode") != "registry"):
            raise PackageResolutionError("APPROVED_ASSIGNMENT_INVALID")
        outputs = self._manifest_outputs(manifest, document_ids, processed_prefix)
        parser = manifest["parser"]
        required = ("key", "blob_path", "sha256", "approval_id", "variant_id", "version")
        if any(not isinstance(parser.get(key), str) for key in required) or parser["key"] != request.parser_key:
            raise PackageResolutionError("APPROVED_ASSIGNMENT_INVALID")
        _ids(parser["variant_id"], parser["version"])
        approval = _json(self.storage, f"approved/{request.parser_key}/{parser['variant_id']}/{parser['version']}/approval.json")
        if (approval.get("approval_id") != parser["approval_id"]
                or approval.get("code_sha256") != parser["sha256"]
                or approval.get("customer") != request.customer_key
                or approval.get("parser_key") != request.parser_key):
            raise PackageResolutionError("APPROVED_ASSIGNMENT_INVALID")
        return {"kind": "approved_manifest", "document_ids": document_ids, "manifest_path": path,
                "manifest_sha256": _hash(manifest), "parser": {key: parser[key] for key in required},
                "outputs": outputs}

    def _approved_selection(self, request: ProcessingRequest, document_ids: list[str],
                            supplied: dict[str, Any]) -> dict[str, Any]:
        required = ("variant_id", "version", "approval_id", "code_sha256")
        if any(not isinstance(supplied.get(key), str) for key in required):
            raise PackageResolutionError("APPROVED_ASSIGNMENT_INVALID")
        _ids(supplied["variant_id"], supplied["version"])
        approval_path = (f"approved/{request.parser_key}/{supplied['variant_id']}/"
                         f"{supplied['version']}/approval.json")
        approval = _json(self.storage, approval_path)
        if (approval.get("approval_id") != supplied["approval_id"]
                or approval.get("code_sha256") != supplied["code_sha256"]
                or approval.get("customer") != request.customer_key
                or approval.get("parser_key") != request.parser_key):
            raise PackageResolutionError("APPROVED_ASSIGNMENT_INVALID")
        return {"kind": "approved_selection", "document_ids": document_ids,
                "variant_id": supplied["variant_id"], "version": supplied["version"],
                "approval_id": supplied["approval_id"], "code_sha256": supplied["code_sha256"]}

    def _approved_selection_outputs(self, plan: dict[str, Any], assignment: dict[str, Any],
                                     mappings: dict[str, dict[str, str]]) -> list[dict[str, Any]]:
        path = (f"{plan['customer_key']}/rfq-{plan['rfq_uuid']}/package-{plan['package_version']}/"
                f"processed/{plan['job_id']}/manifest.json")
        manifest = _json(self.storage, path, output=True)
        if manifest.get("request_fingerprint") != plan["request_fingerprint"]:
            raise PackageResolutionError("APPROVED_ASSIGNMENT_INVALID")
        prefix = self._processed_prefix(plan)
        current = self._manifest_outputs(manifest, assignment["document_ids"], prefix)
        for item in current:
            parser = item.get("parser")
            if parser is None:
                parser = manifest.get("parser", {})
                code_sha256 = parser.get("sha256")
            else:
                code_sha256 = parser.get("code_sha256")
            if (parser.get("variant_id") != assignment["variant_id"]
                    or parser.get("version") != assignment["version"]
                    or parser.get("approval_id") != assignment["approval_id"]
                    or code_sha256 != assignment["code_sha256"]):
                raise PackageResolutionError("APPROVED_ASSIGNMENT_INVALID")
        return self._approved_group("approved_selection", assignment["document_ids"],
                                    path, _hash(manifest), current, mappings, prefix)

    def _approved_outputs(self, plan: dict[str, Any], assignment: dict[str, Any],
                          mappings: dict[str, dict[str, str]]) -> list[dict[str, Any]]:
        manifest = _json(self.storage, assignment["manifest_path"], output=True)
        if _hash(manifest) != assignment["manifest_sha256"]:
            raise PackageResolutionError("APPROVED_ASSIGNMENT_INVALID")
        prefix = self._processed_prefix(plan)
        current = self._manifest_outputs(manifest, assignment["document_ids"], prefix)
        if current != assignment["outputs"]:
            raise PackageResolutionError("APPROVED_ASSIGNMENT_INVALID")
        return self._approved_group("approved_manifest", assignment["document_ids"],
                                    assignment["manifest_path"], _hash(manifest), current, mappings, prefix)

    def _approved_group(self, kind: str, document_ids: list[str], manifest_path: str,
                        manifest_sha256: str, items: list[dict[str, Any]],
                        mappings: dict[str, dict[str, str]], prefix: str) -> list[dict[str, Any]]:
        """Derive and split approved fixtures by byte-verified assembly identity."""
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
        grouped_mappings: dict[tuple[str, str], list[str]] = {}
        mapping_keys: list[str] = []
        for item in items:
            data = self._output_bytes(item["output"], prefix)
            facts = self._fixture_semantics(data)
            key = item["output"]["sha256"]
            if facts["assembly"] is None:
                mapping = mappings.get(key)
                if mapping is None:
                    raise PackageResolutionError("PACKAGE_MAPPING_REQUIRED")
                mapping_keys.append(key)
                identity = AssemblyIdentity(mapping["name"], mapping.get("revision", "")).normalized()
            else:
                if key in mappings:
                    raise PackageResolutionError("PACKAGE_REVIEW_INPUT_INVALID")
                identity = facts["assembly"].normalized()
            grouped.setdefault(identity, []).append({"fixture_key": key, "document_ids": [item["document_id"]],
                "storage": "output", "output": item["output"],
                "output_sha256": facts["output_sha256"],
                "semantic_fingerprint": facts["semantic_fingerprint"],
                "model_summary": facts["model_summary"], "source": item["source"],
                "parser": item.get("parser")})
            if key in mapping_keys:
                grouped_mappings.setdefault(identity, []).append(key)
        result = []
        for identity, fixtures in sorted(grouped.items()):
            docs = sorted({doc for fixture in fixtures for doc in fixture["document_ids"]})
            evidence = {"manifest_path": manifest_path, "manifest_sha256": manifest_sha256,
                        "assembly_identity": {"name": identity[0], "revision": identity[1]},
                        "fixtures": sorted(fixtures, key=lambda item: item["fixture_key"])}
            result.append({"kind": kind, "document_ids": docs, "status": "ready",
                           "evidence": evidence,
                           "mapping_keys": sorted(set(grouped_mappings.get(identity, []))),
                           "disposition_keys": []})
        return result

    def _manifest_outputs(self, manifest: dict[str, Any], document_ids: list[str],
                          prefix: str) -> list[dict[str, Any]]:
        results = manifest.get("results")
        if not isinstance(results, list):
            raise PackageResolutionError("APPROVED_ASSIGNMENT_INVALID")
        require_result_parser = len(results) > 1
        selected = []
        for item in results:
            source = item.get("source") if isinstance(item, dict) else None
            if isinstance(source, dict) and source.get("document_id") in document_ids:
                parser = item.get("parser")
                if require_result_parser or parser is not None:
                    self._validate_result_parser(parser, manifest)
                selected.append({"document_id": source["document_id"], "source": source,
                                 "output": item.get("output"), "parser": parser})
        if sorted(item["document_id"] for item in selected) != document_ids:
            raise PackageResolutionError("APPROVED_ASSIGNMENT_INVALID")
        for item in selected:
            self._output_bytes(item["output"], prefix)
        return sorted(selected, key=lambda item: item["document_id"])

    def _validate_result_parser(self, parser: Any, manifest: dict[str, Any]) -> None:
        keys = {"key", "variant_id", "version", "approval_id", "code_path",
                "code_sha256", "signature_hash", "rule_hash", "registry_generation"}
        if (not isinstance(parser, dict) or set(parser) != keys
                or any(not isinstance(parser.get(key), str) for key in keys - {"registry_generation"})
                or type(parser.get("registry_generation")) is not int
                or parser["key"] != manifest.get("parser", {}).get("key")
                or not all(_HASH.fullmatch(parser[key]) for key in
                           ("code_sha256", "signature_hash", "rule_hash"))):
            raise PackageResolutionError("APPROVED_ASSIGNMENT_INVALID")
        _ids(parser["variant_id"], parser["version"], parser["approval_id"])
        expected_path = (f"approved/{parser['key']}/{parser['variant_id']}/"
                         f"{parser['version']}/parse.py")
        if parser["code_path"] != expected_path:
            raise PackageResolutionError("APPROVED_ASSIGNMENT_INVALID")
        approval = _json(self.storage, expected_path[:-len("parse.py")] + "approval.json")
        if (approval.get("approval_id") != parser["approval_id"]
                or approval.get("code_sha256") != parser["code_sha256"]
                or approval.get("signature_hash") != parser["signature_hash"]
                or approval.get("rule_hash") != parser["rule_hash"]):
            raise PackageResolutionError("APPROVED_ASSIGNMENT_INVALID")

    def _candidate_groups(self, assignment: dict[str, Any], review: dict[str, Any],
                          mappings: dict[str, dict[str, str]]) -> list[dict[str, Any]]:
        request_id = assignment["request_id"]
        candidate_id = review.get("candidate_id")
        validation_id = review.get("validation_id")
        aliases = sorted(str(alias.get("document_id")) for group in review.get("groups", [])
                         for fixture in group.get("fixtures", []) for alias in fixture.get("source_aliases", []))
        if (aliases != assignment["document_ids"] or not review.get("validation_complete")
                or not isinstance(candidate_id, str) or not isinstance(validation_id, str)):
            raise PackageResolutionError("CANDIDATE_SELECTION_INVALID")
        code = self._verified_candidate_code(request_id, candidate_id, review.get("candidate"))
        candidate_prefix = f"candidates/{request_id}/{candidate_id}/validation/{validation_id}/"
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
        grouped_mappings: dict[tuple[str, str], list[str]] = {}
        for group in review.get("groups", []):
            for fixture in group.get("fixtures", []):
                key = fixture["fixture_sha256"]
                data = self._candidate_bytes(fixture.get("output"), candidate_prefix)
                facts = self._fixture_semantics(data)
                claimed = fixture.get("assembly_identity")
                if facts["assembly"] is None:
                    # Bytes carry no assembly identity: a stored claim is never
                    # a substitute; an explicit mapping is required.
                    if isinstance(claimed, dict) and str(claimed.get("name", "")).strip():
                        raise PackageResolutionError("PACKAGE_SEMANTIC_BINDING")
                    mapping = mappings.get(key)
                    if mapping is None:
                        raise PackageResolutionError("PACKAGE_MAPPING_REQUIRED")
                    identity = AssemblyIdentity(mapping["name"], mapping.get("revision", "")).normalized()
                    grouped_mappings.setdefault(identity, []).append(key)
                else:
                    identity = facts["assembly"].normalized()
                    if (not isinstance(claimed, dict)
                            or AssemblyIdentity(str(claimed.get("name", "")),
                                                str(claimed.get("revision", ""))).normalized() != identity):
                        raise PackageResolutionError("PACKAGE_SEMANTIC_BINDING")
                    if key in mappings:
                        raise PackageResolutionError("PACKAGE_REVIEW_INPUT_INVALID")
                if (fixture.get("fingerprint") != facts["semantic_fingerprint"]
                        or fixture.get("model_summary") != facts["model_summary"]):
                    raise PackageResolutionError("PACKAGE_SEMANTIC_BINDING")
                docs = sorted(str(alias.get("document_id")) for alias in fixture.get("source_aliases", []))
                grouped.setdefault(identity, []).append({"fixture_key": key, "document_ids": docs,
                    "storage": "candidate", "output": fixture["output"],
                        "prefix": candidate_prefix,
                    "output_sha256": facts["output_sha256"],
                    "semantic_fingerprint": facts["semantic_fingerprint"],
                    "model_summary": facts["model_summary"]})
        result = []
        for identity, fixtures in sorted(grouped.items()):
            evidence = {"request_id": request_id, "candidate_id": candidate_id,
                        "validation_id": validation_id,
                        "review_manifest_sha256": review.get("evidence", {}).get("manifest_sha256"),
                        "candidate": code,
                        "assembly_identity": {"name": identity[0], "revision": identity[1]},
                        "fixtures": sorted(fixtures, key=lambda item: item["fixture_key"])}
            result.append({"status": "ready", "candidate_id": candidate_id,
                           "validation_id": validation_id, "evidence": evidence,
                           "document_ids": sorted({doc for item in fixtures for doc in item["document_ids"]}),
                           "mapping_keys": sorted(set(grouped_mappings.get(identity, []))),
                           "disposition_keys": []})
        return result

    def _verified_candidate_code(self, request_id: str, candidate_id: str,
                                  ref: Any) -> dict[str, Any]:
        """Verify candidate parse.py bytes against the review's candidate hash/path."""
        if (not isinstance(ref, dict) or not isinstance(ref.get("sha256"), str)
                or not _HASH.fullmatch(ref["sha256"])):
            raise PackageResolutionError("PACKAGE_CANDIDATE_BINDING")
        path = ref.get("blob_path") if isinstance(ref.get("blob_path"), str) else None
        if path is None:
            path = f"candidates/{request_id}/{candidate_id}/parse.py"
        if ("\\" in path or ".." in path.split("/")
                or not path.startswith(f"candidates/{request_id}/{candidate_id}/")
                or not path.endswith("/parse.py")):
            raise PackageResolutionError("PACKAGE_CANDIDATE_BINDING")
        try:
            code = self.storage.download_candidate(path, self.max_parser_bytes)
        except Exception as exc:
            raise PackageResolutionError("PACKAGE_CANDIDATE_BINDING") from exc
        if hashlib.sha256(code).hexdigest() != ref["sha256"]:
            raise PackageResolutionError("PACKAGE_CANDIDATE_BINDING")
        if type(ref.get("size_bytes")) is int and ref["size_bytes"] != len(code):
            raise PackageResolutionError("PACKAGE_CANDIDATE_BINDING")
        return {"blob_path": path, "sha256": hashlib.sha256(code).hexdigest(),
                "size_bytes": len(code)}

    def _fixture_semantics(self, data: bytes) -> dict[str, Any]:
        """Derive semantic facts from verified bytes, including the Document-row assembly."""
        try:
            facts = facts_from_xlsx(data)
        except SemanticFactsError as exc:
            raise PackageResolutionError("PACKAGE_ASSEMBLY_CONFLICT") from exc
        return {"assembly": facts.assembly, "output_sha256": facts.output_sha256,
                "semantic_fingerprint": facts.fingerprint,
                "model_summary": facts.model_summary}

    def _candidate_bytes(self, output: Any, prefix: str) -> bytes:
        return self._verified_output(output, candidate=True, prefix=prefix)

    def _output_bytes(self, output: Any, prefix: str) -> bytes:
        return self._verified_output(output, candidate=False, prefix=prefix)

    def _verified_output(self, output: Any, *, candidate: bool, prefix: str) -> bytes:
        if (not isinstance(output, dict) or output.get("content_type") != OUTPUT_CONTENT_TYPE
                or not isinstance(output.get("blob_path"), str) or "\\" in output["blob_path"]
                or ".." in output["blob_path"].split("/")
                or not isinstance(prefix, str) or not output["blob_path"].startswith(prefix)
                or type(output.get("size_bytes")) is not int or not 0 < output["size_bytes"] <= self.max_output_bytes
                or not _HASH.fullmatch(str(output.get("sha256", "")))):
            raise PackageResolutionError("PACKAGE_OUTPUT_INVALID")
        if candidate and not output["blob_path"].startswith("candidates/"):
            raise PackageResolutionError("PACKAGE_OUTPUT_INVALID")
        data = (self.storage.download_candidate(output["blob_path"], self.max_output_bytes)
                if candidate else self.storage.download_output(output["blob_path"], self.max_output_bytes))
        if data is None or len(data) != output["size_bytes"] or hashlib.sha256(data).hexdigest() != output["sha256"]:
            raise PackageResolutionError("PACKAGE_OUTPUT_INVALID")
        try:
            from .remote import _validate_xlsx_bytes
            _validate_xlsx_bytes(data, self.max_rows)
        except Exception as exc:
            raise PackageResolutionError("PACKAGE_OUTPUT_INVALID") from exc
        return data

    @staticmethod
    def _rows(data: bytes) -> list[dict[str, Any]]:
        import pandas as pd
        return pd.read_excel(io.BytesIO(data), engine="openpyxl").to_dict(orient="records")

    @staticmethod
    def _workbook(rows: list[dict[str, Any]] | tuple[dict[str, Any], ...]) -> bytes:
        """Serialize canonical rows to XLSX bytes deterministically.

        Content-addressed paths alone do not guarantee partial replay because
        openpyxl embeds wall-clock timestamps and unordered ZIP members. The
        workbook properties are pinned and the ZIP container is rewritten with
        fixed member order, timestamps, and metadata, so identical logical rows
        always produce identical bytes.
        """
        import pandas as pd
        normalized = []
        for index, item in enumerate(rows, 1):
            row = {column: item.get(column) for column in CANONICAL_COLUMNS}
            row["line_number"] = index
            normalized.append(row)
        stream = io.BytesIO()
        with pd.ExcelWriter(stream, engine="openpyxl") as writer:
            pd.DataFrame(normalized, columns=CANONICAL_COLUMNS).to_excel(writer, index=False)
            writer.book.properties.created = _WORKBOOK_EPOCH
            writer.book.properties.modified = _WORKBOOK_EPOCH
        return ParentPackageService._normalized_xlsx(stream.getvalue())

    @staticmethod
    def _normalized_xlsx(data: bytes) -> bytes:
        import zipfile
        with zipfile.ZipFile(io.BytesIO(data)) as source:
            members = [(name, source.read(name)) for name in sorted(source.namelist())]
        target_stream = io.BytesIO()
        with zipfile.ZipFile(target_stream, "w", compression=zipfile.ZIP_DEFLATED) as target:
            for name, content in members:
                info = zipfile.ZipInfo(name, date_time=_ZIP_EPOCH)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 0
                info.external_attr = 0
                target.writestr(info, content)
        return target_stream.getvalue()

    @staticmethod
    def _mappings(value: Any) -> dict[str, dict[str, str]]:
        if not isinstance(value, dict):
            raise PackageResolutionError("PACKAGE_REVIEW_INPUT_INVALID")
        result = {}
        for key, item in value.items():
            if (not _HASH.fullmatch(str(key)) or not isinstance(item, dict)
                    or set(item) - {"name", "revision"} or not isinstance(item.get("name"), str)
                    or not item["name"].strip() or not isinstance(item.get("revision", ""), str)):
                raise PackageResolutionError("PACKAGE_REVIEW_INPUT_INVALID")
            result[key] = {"name": item["name"].strip(), "revision": item.get("revision", "").strip()}
        return dict(sorted(result.items()))

    @staticmethod
    def _dispositions(value: Any) -> dict[str, str]:
        if (not isinstance(value, dict) or any(not _ID.fullmatch(str(key)) or not _HASH.fullmatch(str(item))
                                               for key, item in value.items())):
            raise PackageResolutionError("PACKAGE_REVIEW_INPUT_INVALID")
        return dict(sorted(value.items()))

    def _plan(self, plan_id: str) -> dict[str, Any]:
        plan = _json(self.storage, self._plan_path(plan_id))
        identity = {key: value for key, value in plan.items() if key != "plan_id"}
        if plan.get("version") != "package-plan.v1" or plan.get("plan_id") != plan_id or _hash(identity) != plan_id:
            raise PackageResolutionError("PACKAGE_PLAN_BINDING")
        return plan

    def _bound_approval(self, plan_id: str, approval_id: str) -> dict[str, Any]:
        approval = _json(self.storage, self._approval_path(plan_id, approval_id))
        identity = {key: approval.get(key) for key in (
            "version", "plan_id", "review_manifest_sha256", "review", "approver", "reason", "ticket")}
        if (approval.get("version") != "package-approval.v1"
                or approval.get("plan_id") != plan_id
                or approval.get("approval_id") != approval_id or _hash(identity) != approval_id):
            raise PackageResolutionError("PACKAGE_APPROVAL_BINDING")
        return approval

    def _validate_resolved(self, raw: bytes, plan: dict[str, Any], approval: dict[str, Any],
                           prefix: str) -> dict[str, Any]:
        try:
            manifest = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            raise PackageResolutionError("RESOLVED_OUTPUT_CONFLICT") from exc
        if not isinstance(manifest, dict):
            raise PackageResolutionError("RESOLVED_OUTPUT_CONFLICT")
        if set(manifest) != {"version", "status", "plan_id", "approval_id", "plan_sha256",
                            "review_manifest_sha256", "results", "manifest_sha256"}:
            raise PackageResolutionError("RESOLVED_OUTPUT_CONFLICT")
        identity = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
        results = manifest.get("results")
        if (manifest.get("version") != "resolved-output-manifest.v1"
                or manifest.get("status") != "completed"
                or manifest.get("plan_id") != plan.get("plan_id")
                or manifest.get("approval_id") != approval.get("approval_id")
                or manifest.get("plan_sha256") != _hash({key: value for key, value in plan.items() if key != "plan_id"})
                or manifest.get("review_manifest_sha256") != approval.get("review", {}).get("manifest_sha256")
                or manifest.get("manifest_sha256") != _hash(identity)
                or not isinstance(results, list)):
            raise PackageResolutionError("RESOLVED_OUTPUT_CONFLICT")
        review_groups = {item["group_id"]: item for item in approval.get("review", {}).get("groups", [])
                 if isinstance(item, dict) and isinstance(item.get("group_id"), str)}
        covered: list[str] = []
        paths: dict[str, dict[str, Any]] = {}
        seen_groups: set[str] = set()
        for result in results:
            if (not isinstance(result, dict)
                or set(result) != {"group_id", "document_ids", "assembly_identity",
                          "reconciliation", "evidence_sha256", "provenance", "output"}
                or not isinstance(result.get("document_ids"), list)
                    or not result["document_ids"]
                    or any(not isinstance(item, str) for item in result["document_ids"])
                    or result.get("reconciliation") not in _RECONCILIATIONS
                    or not _HASH.fullmatch(str(result.get("evidence_sha256", "")))):
                raise PackageResolutionError("RESOLVED_OUTPUT_CONFLICT")
            group = review_groups.get(result.get("group_id"))
            if group is None or result["group_id"] in seen_groups:
                raise PackageResolutionError("RESOLVED_OUTPUT_CONFLICT")
            seen_groups.add(result["group_id"])
            evidence = group.get("evidence", {})
            if (result["document_ids"] != group.get("document_ids")
                    or result["assembly_identity"] != evidence.get("assembly_identity")
                    or result["evidence_sha256"] != _hash(evidence)
                    or result["provenance"] != self._result_provenance(evidence)):
                raise PackageResolutionError("RESOLVED_OUTPUT_CONFLICT")
            expected_relation = ("human_selected_conflict"
                                 if evidence.get("reconciliation") == "conflict"
                                 else evidence.get("reconciliation"))
            if result["reconciliation"] != expected_relation:
                raise PackageResolutionError("RESOLVED_OUTPUT_CONFLICT")
            covered.extend(result["document_ids"])
            output = result.get("output")
            path = output.get("blob_path") if isinstance(output, dict) else None
            if (not isinstance(output, dict)
                    or set(output) != {"blob_path", "sha256", "size_bytes", "content_type"}
                    or not isinstance(path, str) or not path.startswith(f"{prefix}/objects/")
                    or not path.endswith(f"/{output.get('sha256')}.xlsx")):
                raise PackageResolutionError("RESOLVED_OUTPUT_CONFLICT")
            prior = paths.get(path)
            if prior is not None and prior != output:
                raise PackageResolutionError("RESOLVED_OUTPUT_CONFLICT")
            paths[path] = output
            try:
                self._output_bytes(output, f"{prefix}/objects/")
            except PackageResolutionError as exc:
                raise PackageResolutionError("RESOLVED_OUTPUT_CONFLICT") from exc
        expected = [item["document_id"] for item in plan["sources"]]
        if (results != sorted(results, key=lambda item: item["document_ids"])
                or sorted(covered) != sorted(expected) or len(covered) != len(set(covered))
            or seen_groups != set(review_groups)):
            raise PackageResolutionError("RESOLVED_OUTPUT_CONFLICT")
        return manifest

    @staticmethod
    def _plan_path(plan_id: str) -> str:
        return f"packages/{plan_id}/package-plan.v1.json"

    @staticmethod
    def _approval_path(plan_id: str, approval_id: str) -> str:
        return f"packages/{plan_id}/approvals/{approval_id}.json"

    @staticmethod
    def _resolved_prefix(plan_id: str, approval_id: str) -> str:
        return f"packages/{plan_id}/resolved/{approval_id}"
