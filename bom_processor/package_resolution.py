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
from .semantic_facts import facts_from_xlsx

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_MAX_METADATA = 1_000_000
_RECONCILIATIONS = frozenset({"equivalent", "merged", "human_selected_conflict", "primary_selected"})
_ZIP_EPOCH = (2000, 1, 1, 0, 0, 0)
# Sub-assembly plan unit 3b: v2 adds a structure file to groups whose BOM states parents; a package
# with no such group is still published as v1, byte for byte as before.
MANIFEST_V1, MANIFEST_V2 = "resolved-output-manifest.v1", "resolved-output-manifest.v2"
STRUCTURE_VERSION = "resolved-bom-structure.v1"
STRUCTURE_CONTENT_TYPE = "application/json"
_RESULT_KEYS = frozenset({"group_id", "document_ids", "assembly_identity", "reconciliation",
                          "evidence_sha256", "provenance", "output"})
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
             pending_assignments: list[dict[str, Any]],
             package_choice: dict[str, Any] | None = None) -> dict[str, Any]:
        """Persist an immutable inventory after independently verifying every source.

        package_choice is a completed bom_package_choice status; the documents it set aside
        become excluded assignments. Without it the plan is exactly as before.
        """
        request.validate()
        sources = self._verified_sources(request)
        processed_prefix = self._request_processed_prefix(request)
        assignments: list[dict[str, Any]] = []
        covered: list[str] = []
        choice_ref = None
        if package_choice is not None:
            from .bom_package_choice import choice_reference, excluded_assignments
            if (package_choice.get("status") != "completed" or request.package_files is None
                    or package_choice.get("reopened_document_ids") != (
                        None if request.package_choice_reopen is None else list(request.package_choice_reopen))):
                raise PackageResolutionError("PACKAGE_CHOICE_BINDING")
            choice_ref = choice_reference(package_choice)
            for item in excluded_assignments(package_choice):
                document_ids = self._documents(item, sources)
                assignments.append({key: value for key, value in item.items() if key != "package_choice"}
                                   | {"document_ids": document_ids})
                covered.extend(document_ids)
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
        if choice_ref is not None:
            identity["package_choice"] = choice_ref
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
        primaries = self._primary_selections(plan)
        choice_result = None
        if plan.get("package_choice") is not None:
            from .bom_package_choice import bound_state
            choice_result = bound_state(self.storage, plan["package_choice"])["result"]
        for assignment in plan["assignments"]:
            if assignment["kind"] == "excluded":
                # Set aside by the package choice; recorded, never parsed (plan 8.2-2, 8.2-9).
                groups.append({"kind": "excluded", "document_ids": assignment["document_ids"],
                               "status": "excluded", "reason": assignment["reason"],
                               "roles": assignment["roles"], "assemblies": assignment["assemblies"],
                               "mapping_keys": [], "disposition_keys": []})
                continue
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
            if child.get("automation_error") is not None:
                projected["automation_error"] = child["automation_error"]
                if choice_result is not None:
                    documents = [item for item in choice_result["documents"]
                                 if item["document_id"] in assignment["document_ids"]]
                    file_ids = {file_id for item in documents for file_id in item["survey_file_ids"]}
                    projected["source_file_paths"] = sorted({item["path"] for item in choice_result["survey"]["files"]
                                                              if item["file_id"] in file_ids})
                    projected["source_assemblies"] = sorted({name for item in documents for name in item["assemblies"]})
            if child.get("review_error") == "review_semantic_binding":
                raise PackageResolutionError("PACKAGE_SEMANTIC_BINDING")
            if child.get("status") not in {"awaiting_approval", "approved"} or not isinstance(child.get("review"), dict):
                complete = False
                projected.update({"status": "pending", "mapping_keys": [], "disposition_keys": []})
            else:
                candidate_groups = self._candidate_groups(assignment, child["review"], mappings, choice_result, plan=plan)
                groups.extend([{**projected, **item} for item in candidate_groups])
                complete &= all(item["status"] == "ready" for item in candidate_groups)
                continue
            groups.append(projected)
        groups = self._reconcile_review_groups(groups, dispositions, primaries)
        linked = choice_result is not None and "requested_assemblies" in choice_result
        from .package_attachment import attach_files
        groups, subassembly_findings = attach_files(self, plan, groups,
                                                    choice_result["requested_assemblies"] if linked else None)
        if linked:
            groups = requested_links(groups, choice_result)
        complete &= all(group.get("status") in ("ready", "excluded") for group in groups)
        used_mappings = {key for group in groups for key in group.get("mapping_keys", [])}
        used_dispositions = {key for group in groups for key in group.get("disposition_keys", [])}
        if used_mappings != set(mappings) or not set(dispositions) <= used_dispositions:
            raise PackageResolutionError("PACKAGE_REVIEW_INPUT_INVALID")
        # Decisions are all given (a preview or an approval) or none (the page's review); a review still
        # waiting for a pick is complete but not approval-ready.
        pending = used_dispositions - set(dispositions)
        if pending and dispositions:
            raise PackageResolutionError("PACKAGE_CONFLICT_DISPOSITION_REQUIRED")
        ready = complete and not pending
        body = {"version": "package-review.v1", "plan_id": plan_id,
                "plan_sha256": _hash({key: value for key, value in plan.items() if key != "plan_id"}),
                "complete": complete, "approval_ready": ready,
                "status": "awaiting_approval" if ready else "pending",
                "source_count": len(plan["sources"]), "groups": groups,
                "assembly_mappings": mappings, "conflict_dispositions": dispositions}
        if subassembly_findings:
            body["subassembly_findings"] = subassembly_findings
        if choice_result is not None:
            failures = [item for item in choice_result["findings"]
                        if item["code"] in PACKAGE_CHOICE_FINDING_CODES and item.get("raised_by") == "code"]
            if failures:
                body["package_choice_findings"] = failures
        if linked:
            # One row per surveyed file for the person's review (round 2); bound by manifest_sha256.
            body["requested_assemblies"] = choice_result["requested_assemblies"]
            body["package_files"] = package_file_rows(choice_result)
        return {**body, "manifest_sha256": _hash(body)}

    def approve(self, *, plan_id: str, review_manifest_sha256: str,
                approver: str, assembly_mappings: dict[str, dict[str, str]] | None = None,
                conflict_dispositions: dict[str, str] | None = None,
                reason: str = "", ticket: str = "", caller_application: str | None = None) -> dict[str, Any]:
        from .package_writer_gate import package_writer
        from .package_writer_gate import assert_plan_coordination
        assert_plan_coordination(self.storage, self._plan(plan_id))
        with package_writer(self.storage, plan_id, "approval") as guard:
            return self._approve_locked(plan_id=plan_id, review_manifest_sha256=review_manifest_sha256,
                approver=approver, assembly_mappings=assembly_mappings,
            conflict_dispositions=conflict_dispositions, reason=reason, ticket=ticket,
            caller_application=caller_application, _guard=guard)

    def _approve_locked(self, *, plan_id: str, review_manifest_sha256: str,
                approver: str, assembly_mappings: dict[str, dict[str, str]] | None = None,
                conflict_dispositions: dict[str, str] | None = None,
                reason: str = "", ticket: str = "", caller_application: str | None = None, _guard=None) -> dict[str, Any]:
        _ids(plan_id, approver)
        if caller_application is not None:
            _ids(caller_application)
        if (not _HASH.fullmatch(str(review_manifest_sha256)) or not isinstance(reason, str)
                or len(reason) > 2000 or not isinstance(ticket, str) or len(ticket) > 256):
            if _guard is not None:
                _guard.reject(PackageResolutionError("PACKAGE_APPROVAL_INVALID"))
            raise PackageResolutionError("PACKAGE_APPROVAL_INVALID")
        review = self.review(plan_id, assembly_mappings=assembly_mappings,
                             conflict_dispositions=conflict_dispositions)
        if not review["approval_ready"] or review["manifest_sha256"] != review_manifest_sha256:
            if _guard is not None:
                _guard.reject(PackageResolutionError("PACKAGE_REVIEW_BINDING"))
            raise PackageResolutionError("PACKAGE_REVIEW_BINDING")
        identity = {"version": "package-approval.v1", "plan_id": plan_id,
                    "review_manifest_sha256": review_manifest_sha256, "review": review,
                    "approver": approver, "reason": reason.strip(), "ticket": ticket.strip()}
        if caller_application is not None:
            identity["caller_application"] = caller_application
        approval_id = _hash(identity)
        now = self.clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        record = {**identity, "approval_id": approval_id,
                  "approved_time": now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")}
        path = self._approval_path(plan_id, approval_id)
        if _guard is not None:
            _guard.write_attempted = True
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
        results = []
        for group in review["groups"]:
            if group.get("status") == "excluded":
                continue
            evidence = group["evidence"]
            view = self._reconciled(plan, evidence)
            rows, relation = view["rows"], view["relation"]
            if any(entry["action"] == "attached" for entry in evidence.get("subassembly_files", [])):
                # Unit 6: attached sub-assembly files go under their lines; one structure for all.
                from .package_attachment import structure as attached_structure
                rows, structure = attached_structure(self, plan, group)
            else:
                index = view["index"]
                structure = self._group_structure(plan, group, view["fixtures"][index], view["fixture_bytes"][index],
                                                  view["row_sets"][index], view["origins"])
            output = self._workbook(rows)
            digest = hashlib.sha256(output).hexdigest()
            output_path = f"{prefix}/objects/{digest}.xlsx"
            self.storage.upload_output_immutable(output_path, output, OUTPUT_CONTENT_TYPE)
            result = {"group_id": group["group_id"],
                      "document_ids": group["document_ids"],
                      **({"subassembly_document_ids": group["subassembly_document_ids"]}
                         if "subassembly_document_ids" in group else {}),
                      "assembly_identity": evidence["assembly_identity"],
                      "reconciliation": relation,
                      "evidence_sha256": _hash(evidence),
                      "provenance": self._result_provenance(evidence),
                      "output": {"blob_path": output_path, "sha256": digest,
                                 "size_bytes": len(output), "content_type": OUTPUT_CONTENT_TYPE}}
            if structure is not None:
                data = _json_bytes(structure)
                structure_digest = hashlib.sha256(data).hexdigest()
                structure_path = f"{prefix}/objects/{structure_digest}.json"
                self.storage.upload_output_immutable(structure_path, data, STRUCTURE_CONTENT_TYPE)
                result["structure"] = {"blob_path": structure_path, "sha256": structure_digest,
                                       "size_bytes": len(data), "content_type": STRUCTURE_CONTENT_TYPE}
            results.append(result)
        results.sort(key=lambda item: (item["document_ids"], item["group_id"]))
        version = MANIFEST_V2 if any("structure" in item for item in results) else MANIFEST_V1
        manifest_identity = {"version": version, "status": "completed",
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

    def resolved_structure(self, plan_id: str, approval_id: str, group_id: str) -> bytes:
        """Return one byte-verified published structure file (unit 3b) without writing state.

        The stored bytes are returned unchanged so a caller can check them against the manifest hash.
        """
        _ids(plan_id, approval_id, group_id)
        manifest = self.manifest(plan_id, approval_id)
        matches = [item for item in manifest["results"]
                   if isinstance(item, dict) and item.get("group_id") == group_id]
        if len(matches) != 1:
            raise PackageResolutionError("RESOLVED_OUTPUT_GROUP_INVALID")
        if "structure" not in matches[0]:
            raise PackageResolutionError("RESOLVED_STRUCTURE_ABSENT")
        prefix = f"{self._resolved_prefix(plan_id, approval_id)}/objects/"
        return self._structure_bytes(matches[0]["structure"], prefix)

    def _structure_bytes(self, ref: Any, prefix: str) -> bytes:
        if (not isinstance(ref, dict) or set(ref) != {"blob_path", "sha256", "size_bytes", "content_type"}
                or ref.get("content_type") != STRUCTURE_CONTENT_TYPE
                or not _HASH.fullmatch(str(ref.get("sha256", "")))
                or ref.get("blob_path") != prefix + str(ref.get("sha256")) + ".json"
                or type(ref.get("size_bytes")) is not int or not 0 < ref["size_bytes"] <= self.max_output_bytes):
            raise PackageResolutionError("RESOLVED_OUTPUT_CONFLICT")
        data = self.storage.download_output(ref["blob_path"], self.max_output_bytes)
        if data is None or len(data) != ref["size_bytes"] or hashlib.sha256(data).hexdigest() != ref["sha256"]:
            raise PackageResolutionError("RESOLVED_OUTPUT_CONFLICT")
        return data

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
            approval = self._bound_approval(plan_id, approval_id)
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

    def _primary_selections(self, plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """Primary document id -> selection, from the plan's package choice (re-verified)."""
        ref = plan.get("package_choice")
        if ref is None:
            return {}
        from .bom_package_choice import PackageChoiceError, bound_state, excluded_assignments
        try:
            state = bound_state(self.storage, ref)
        except PackageChoiceError as exc:
            raise PackageResolutionError("PACKAGE_CHOICE_BINDING") from exc
        expected = [{key: value for key, value in item.items() if key != "package_choice"}
                    for item in excluded_assignments(state)]
        recorded = [item for item in plan["assignments"] if item["kind"] == "excluded"]
        if sorted(expected, key=lambda item: item["document_ids"]) != recorded:
            raise PackageResolutionError("PACKAGE_CHOICE_BINDING")
        choices = {}
        for item in state["result"]["choices"]:
            if item["assembly"] in choices:
                raise PackageResolutionError("PACKAGE_CHOICE_BINDING")
            choices[item["assembly"]] = item
        selections = {}
        for entry in state["result"]["primary_selections"]:
            if entry["excluded_document_ids"] and len(entry["primary_document_ids"]) == 1:
                if entry["assembly"] not in choices:
                    raise PackageResolutionError("PACKAGE_CHOICE_BINDING")
                selections[entry["primary_document_ids"][0]] = {
                    "selection": {"assembly": entry["assembly"],
                                  "excluded_document_ids": entry["excluded_document_ids"],
                                  "package_choice": {"request_id": ref["request_id"],
                                                     "result_sha256": ref["result_sha256"]}},
                    "choice": choices[entry["assembly"]],
                    "documents": {file_id: item["document_id"] for item in state["result"]["documents"]
                                  for file_id in item["survey_file_ids"]}}
        return selections

    def _reconcile_review_groups(self, groups: list[dict[str, Any]],
                                 dispositions: dict[str, str],
                                 primaries: dict[str, dict[str, Any]] | None = None) -> list[dict[str, Any]]:
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
                key = (fixture["storage"], fixture["output"]["blob_path"],
                       (fixture.get("subset") or {}).get("assembly_id"))
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
                row_sets.append(self._fixture_rows(fixture, data))
            reconciliation = reconcile_boms(row_sets, assembly=AssemblyIdentity(*identity))
            group_id = _hash({"assembly_identity": identity})
            disposition = dispositions.get(group_id)
            disposition_keys: list[str] = []
            if reconciliation.relation == "conflict":
                # Without a person's pick the group is shown with its choices (round 13, A044: the review
                # used to fail with 409, so the page could never offer the choice). A pick that is not
                # one of the parsed sources is still refused.
                if disposition is not None and disposition not in {item["fixture_key"] for item in fixtures}:
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
            selection = (primaries or {}).get(evidence["sources"][0]) if len(evidence["sources"]) == 1 else None
            if selection is not None and reconciliation.relation == "equivalent":
                # One primary chosen by the package choice and agreed by the cross-check (plan 8.2-8).
                evidence["reconciliation"] = "primary_selected"
                evidence["primary_selection"] = selection["selection"]
                evidence["post_parse"] = self._post_parse(selection["choice"], selection["documents"],
                                                          reconciliation.rows)
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
    def _post_parse(entry: dict[str, Any], documents: dict[str, str], rows: Any) -> dict[str, Any]:
        """Parsed primary vs the stored identity-column digests of every file set aside (plan 8.3).

        Counts only. needs_review is a warning: a person must approve (system acceptance refuses)
        and may bring a set-aside file back into processing.
        """
        from .bom_candidate_choice import ChoiceError, post_parse_check
        try:
            result = post_parse_check(entry["choice"], None, rows, digests=entry["set_aside_digests"])
        except (ChoiceError, KeyError, TypeError) as exc:
            raise PackageResolutionError("PACKAGE_CHOICE_BINDING") from exc
        return {"version": result["version"], "agreement": result["agreement"],
                "review_reasons": result["review_reasons"],
                "checks": [{**({"document_id": documents[item["file_id"]]} if item["file_id"] in documents else {}),
                            **{key: item[key] for key in ("disposition", "parsed_field", "relation", "outcome",
                                                         "parsed_values", "column_values",
                                                         "column_values_not_in_primary")}}
                           for item in result["checks"]]}

    @staticmethod
    def _result_provenance(evidence: dict[str, Any]) -> dict[str, Any]:
        return {"sources": evidence["sources"],
                "fixtures": [{key: item.get(key) for key in (
                    "fixture_key", "document_ids", "storage", "output_sha256",
                    "semantic_fingerprint", "model_summary", "source", "parser", "prefix", "subset")
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
                          mappings: dict[str, dict[str, str]],
                          choice_result: dict[str, Any] | None = None,
                          plan: dict[str, Any] | None = None) -> list[dict[str, Any]]:
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
                association = fixture.get("assembly_association")
                if association is not None:
                    from pathlib import PurePosixPath
                    from .assembly_association import verify_association
                    from .semantic_facts import facts_from_rows
                    request = _json(self.storage, f"candidates/requests/{request_id}.json")
                    sources = [source for source in request.get("source_evidence", [])
                               if source.get("sha256") == key]
                    try:
                        source = sources[0]
                        original = self.storage.download_source(source["blob_path"], self.max_source_bytes)
                        if len(original) != source["size_bytes"] or hashlib.sha256(original).hexdigest() != key:
                            raise ValueError("source binding")
                        verified = verify_association(original, PurePosixPath(source["blob_path"]).suffix.lower(),
                                                      association["proposal"])
                        if verified != association or verified["has_blocking_findings"]:
                            raise ValueError("association binding")
                    except Exception:
                        raise PackageResolutionError("PACKAGE_ASSOCIATION_BINDING") from None
                    parsed = facts_from_rows(self._rows(data), output_sha256=hashlib.sha256(data).hexdigest(),
                                             infer_document_assembly=False)
                    identity_value = verified["assembly_identity"]
                    facts = {"assembly": AssemblyIdentity(identity_value["name"], identity_value["revision"]),
                             "output_sha256": parsed.output_sha256, "semantic_fingerprint": parsed.fingerprint,
                             "model_summary": parsed.model_summary}
                else:
                    facts = self._fixture_semantics(data)
                claimed = fixture.get("assembly_identity")
                docs = sorted(str(alias.get("document_id")) for alias in fixture.get("source_aliases", []))
                entry = {"fixture_key": key, "document_ids": docs, "storage": "candidate", "output": fixture["output"],
                         "prefix": candidate_prefix, "output_sha256": facts["output_sha256"],
                         "semantic_fingerprint": facts["semantic_fingerprint"], "model_summary": facts["model_summary"],
                         **({"assembly_association": association} if association is not None else {})}
                subsets = (self._combined_subsets(plan, {"evidence": {}}, entry, data, choice_result)
                           if plan is not None else None)
                if subsets:
                    # A045 (Nick 2026-10-13): one BOM holding several requested assemblies. The whole file
                    # must still match what validation recorded; then each assembly gets only its own lines.
                    if (fixture.get("fingerprint") != facts["semantic_fingerprint"]
                            or fixture.get("model_summary") != facts["model_summary"]):
                        raise PackageResolutionError("PACKAGE_SEMANTIC_BINDING")
                    if key in mappings:
                        raise PackageResolutionError("PACKAGE_REVIEW_INPUT_INVALID")
                    rows = self._rows(data)
                    for subset in subsets:
                        head = rows[subset["head_row"] - 2]
                        grouped.setdefault(AssemblyIdentity(_cell_text(head.get("item_number")),
                                                            _cell_text(head.get("revision"))).normalized(),
                                           []).append({**entry, "subset": subset})
                    continue
                # Round 5 (Nick 2026-10-11): with requested assemblies, read identity as a person would:
                # the BOM's own line naming a requested assembly, else the code-confirmed survey link.
                requested = (requested_identity(choice_result, docs, self._rows(data), facts["assembly"])
                             if choice_result is not None and "requested_assemblies" in choice_result else None)
                if requested is not None:
                    # The stored claim still has to match what the bytes say before it is replaced.
                    if facts["assembly"] is None:
                        if isinstance(claimed, dict) and str(claimed.get("name", "")).strip():
                            raise PackageResolutionError("PACKAGE_SEMANTIC_BINDING")
                    elif (not isinstance(claimed, dict)
                            or AssemblyIdentity(str(claimed.get("name", "")),
                                                str(claimed.get("revision", ""))).normalized()
                            != facts["assembly"].normalized()):
                        raise PackageResolutionError("PACKAGE_SEMANTIC_BINDING")
                    if key in mappings:
                        raise PackageResolutionError("PACKAGE_REVIEW_INPUT_INVALID")
                    identity = requested.normalized()
                elif facts["assembly"] is None:
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
                grouped.setdefault(identity, []).append(entry)
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

    def _combined_subsets(self, plan: dict[str, Any], group: dict[str, Any], fixture: dict[str, Any], data: bytes,
                          choice_result: dict[str, Any] | None) -> list[dict[str, Any]] | None:
        """Subsets of a combined BOM (bom_split.py) from parents proven against the source, or None.

        Needs requested assemblies (a package choice) and a row map whose parent claims pass the
        re-check; anything else is today's behaviour. A combined BOM that cannot be divided safely
        stops review with its SPLIT_ code for a person.
        """
        from .bom_split import SplitError, split_rows
        if choice_result is None or not choice_result.get("requested_assemblies"):
            return None
        claimed = self._fixture_claims(plan, group, fixture, data)
        if claimed is None:
            return None
        ref, claims, proven = claimed
        if not proven:
            return None
        rows = self._rows(data)
        numbers = {index + 2: _cell_text(row.get("item_number")) for index, row in enumerate(rows)}
        if set(claims) != set(numbers):
            return None
        try:
            subsets = split_rows(numbers, {row: claims[row] for row in numbers}, choice_result["requested_assemblies"])
        except SplitError as exc:
            raise PackageResolutionError(str(exc)) from None
        return None if subsets is None else [{**item, "row_map_sha256": ref["sha256"]} for item in subsets]

    def _fixture_rows(self, fixture: dict[str, Any], data: bytes) -> list[dict[str, Any]]:
        """A fixture's parsed rows; for a combined BOM only its subset's lines, in file order."""
        rows = self._rows(data)
        subset = fixture.get("subset")
        if not isinstance(subset, dict):
            return rows
        try:
            return [rows[row - 2] for row in subset["output_rows"]]
        except (IndexError, KeyError, TypeError) as exc:
            raise PackageResolutionError("PACKAGE_SPLIT_INVALID") from exc

    def _reconciled(self, plan: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any]:
        """The rows one group publishes: reconciled, or a person's pick in a conflict; with the fixtures.

        origins map each published row to the chosen BOM's row; source_order lists published positions in
        that BOM's own order (parents before children), or None when the rows are not one BOM's rows.
        """
        processed_prefix = self._processed_prefix(plan)
        fixtures = evidence["fixtures"]
        fixture_bytes = [self._candidate_bytes(item["output"], item["prefix"])
                         if item["storage"] == "candidate"
                         else self._output_bytes(item["output"], processed_prefix)
                         for item in fixtures]
        row_sets = [self._fixture_rows(item, data) for item, data in zip(fixtures, fixture_bytes)]
        assembly = evidence["assembly_identity"]
        reconciliation = reconcile_boms(row_sets, assembly=AssemblyIdentity(
            assembly["name"], assembly.get("revision", "")))
        disposition = evidence.get("conflict_disposition")
        if reconciliation.relation == "conflict":
            if disposition not in {item["fixture_key"] for item in fixtures}:
                raise PackageResolutionError("PACKAGE_MATERIAL_CONFLICT")
            index = next(i for i, item in enumerate(fixtures) if item["fixture_key"] == disposition)
            rows = list(row_sets[index])
            relation = "human_selected_conflict"
            origins = list(range(len(rows)))
        else:
            index = 0
            rows, relation = list(reconciliation.rows), reconciliation.relation
            origins = list(reconciliation.origins)
            if evidence.get("reconciliation") == "primary_selected" and relation == "equivalent":
                relation = "primary_selected"
        published = {source_index: position for position, source_index in enumerate(origins)}
        source_order = ([published[q] for q in range(len(row_sets[index]))]
                        if sorted(published) == list(range(len(row_sets[index]))) else None)
        return {"rows": rows, "relation": relation, "index": index, "fixtures": fixtures,
                "fixture_bytes": fixture_bytes, "row_sets": row_sets, "origins": origins,
                "source_order": source_order}

    def _published_parents(self, plan: dict[str, Any], group: dict[str, Any],
                           view: dict[str, Any] | None) -> tuple[dict[str, Any] | None, dict[int, int | None], bool]:
        """(row map ref, published position -> parent position, proven) of a group's chosen BOM (unit 6).

        A flat BOM has no parents and nothing to prove; claims that fail the re-check give no parents.
        """
        if view is None:
            return None, {}, True
        index = view["index"]
        claimed = self._fixture_claims(plan, group, view["fixtures"][index], view["fixture_bytes"][index])
        if claimed is None:
            ref = (self._fixture_row_accounting(group, view["fixtures"][index]) or {}).get("row_map")
            return (ref if isinstance(ref, dict) else None), {}, True
        ref, claims, proven = claimed
        if view["source_order"] is None:
            raise PackageResolutionError("PACKAGE_STRUCTURE_INVALID")
        if not proven:
            return ref, {}, False
        position = view["source_order"]
        subset = view["fixtures"][index].get("subset")
        original = subset["output_rows"] if isinstance(subset, dict) else list(range(2, len(position) + 2))
        index_of = {row: offset for offset, row in enumerate(original)}
        parents: dict[int, int | None] = {}
        for row, parent in claims.items():
            if isinstance(subset, dict) and row not in index_of:
                continue  # another assembly's line in the same combined BOM
            if (type(row) is not int or row not in index_of or len(original) != len(position)
                    or (parent is not None and (type(parent) is not int or parent not in index_of))):
                raise PackageResolutionError("PACKAGE_STRUCTURE_INVALID")
            parents[position[index_of[row]]] = None if parent is None else position[index_of[parent]]
        return ref, parents, True

    def _fixture_claims(self, plan: dict[str, Any], group: dict[str, Any], fixture: dict[str, Any],
                        data: bytes) -> tuple[dict[str, Any], dict[int, int | None], bool] | None:
        """(row map ref, output row -> claimed parent row, proven), or None when the BOM claims no parents.

        The row map is read again and re-checked against the source (a recorded verdict is never trusted).
        """
        from .row_accounting import MAX_ROW_MAP_BYTES, verify_row_map
        accounting = self._fixture_row_accounting(group, fixture)
        ref = accounting.get("row_map") if isinstance(accounting, dict) else None
        if not isinstance(ref, dict):
            return None
        reader = self.storage.download_candidate if fixture["storage"] == "candidate" else self.storage.download_output
        try:
            mapped = reader(ref["blob_path"], MAX_ROW_MAP_BYTES)
            if (mapped is None or len(mapped) != ref["size"]
                    or hashlib.sha256(mapped).hexdigest() != ref["sha256"]):
                raise ValueError("row map binding")
            proposal = json.loads(mapped.decode("utf-8"))
        except Exception as exc:
            raise PackageResolutionError("PACKAGE_ROW_MAP_INVALID") from exc
        claims = {line["output_row"]: line["parent"].get("line") for line in proposal.get("lines", [])
                  if isinstance(line, dict) and isinstance(line.get("parent"), dict)}
        if not claims:
            return None
        source, suffix = self._fixture_source(plan, fixture)
        try:
            verified = verify_row_map(source, suffix, data, mapped)
        except Exception:
            verified = {"valid": False}
        proven = verified.get("valid") is True and (verified.get("structure") or {}).get("by") is not None
        return ref, claims, proven

    def _group_structure(self, plan: dict[str, Any], group: dict[str, Any], fixture: dict[str, Any],
                         data: bytes, rows: list[dict[str, Any]], origins: list[int]) -> dict[str, Any] | None:
        """Unit 3b: the sub-assembly structure of the BOM whose rows are published; None when flat.

        The row map is read again and re-checked against the source here (a recorded verdict is
        never trusted); parents are used only when that check passes, otherwise the structure is
        published as not proven. Lines are given by published line number (origins map the
        re-sorted rows back to this BOM's rows).
        """
        from . import bom_structure
        claimed = self._fixture_claims(plan, group, fixture, data)
        if claimed is None:
            return None
        ref, claims, proven = claimed
        # A combined BOM's subset keeps the file's own output rows, which its parent claims name.
        original = (fixture["subset"]["output_rows"] if isinstance(fixture.get("subset"), dict)
                    else list(range(2, len(rows) + 2)))
        index_of = {row: index for index, row in enumerate(original)}
        lines = [bom_structure.Line(original[index], _cell_text(row.get("item_number")), _cell_text(row.get("item_name")),
                                    _cell_text(row.get("item_category")), _cell(row.get("quantity")),
                                    any(_cell(value) is not None for key, value in row.items()
                                        if str(key).startswith("manufacturer_item_number_")))
                 for index, row in enumerate(rows)]
        parents = {line.output_row: claims.get(line.output_row) for line in lines}
        try:
            resolved = bom_structure.resolve(lines, parents, proven=proven,
                                             assembly=group["evidence"]["assembly_identity"].get("name"))
        except ValueError as exc:
            raise PackageResolutionError("PACKAGE_STRUCTURE_INVALID") from exc
        published = {source_index: position + 1 for position, source_index in enumerate(origins)}
        if sorted(published) != list(range(len(rows))):
            raise PackageResolutionError("PACKAGE_STRUCTURE_INVALID")
        out = []
        for line in resolved["lines"]:
            parent = line["parent_line"]
            item = {key: value for key, value in line.items() if key not in ("output_row", "parent_line")}
            item["line_number"] = published[index_of[line["output_row"]]]
            item["parent_line_number"] = None if parent is None else published[index_of[parent]]
            out.append(item)
        out.sort(key=lambda item: item["line_number"])
        return {"version": STRUCTURE_VERSION, "group_id": group["group_id"],
                "assembly_identity": group["evidence"]["assembly_identity"],
                "row_map_sha256": ref["sha256"], "proven": proven, "lines": out,
                "findings": resolved["findings"], "summary": resolved["summary"]}

    def _fixture_row_accounting(self, group: dict[str, Any], fixture: dict[str, Any]) -> dict[str, Any] | None:
        """The recorded row-accounting summary (with its row-map reference) for one fixture."""
        try:
            if fixture["storage"] == "candidate":
                evidence = _json(self.storage, f"{fixture['prefix']}evidence.json", candidate=True)
                records = evidence.get("fixtures")
                if isinstance(records, list) and any(isinstance(r, dict) and "fixture_sha256" in r for r in records):
                    record = next((r for r in records if isinstance(r, dict)
                                   and r.get("fixture_sha256") == fixture["fixture_key"]), None)
                else:
                    record = evidence
                semantic = record.get("semantic") if isinstance(record, dict) else None
                return semantic.get("row_accounting") if isinstance(semantic, dict) else None
            for child in group["evidence"].get("children", []):
                path = child.get("manifest_path")
                raw = self.storage.download_output(path, self.max_output_bytes) if isinstance(path, str) else None
                manifest = json.loads(raw.decode("utf-8")) if raw is not None else {}
                for result in manifest.get("results", []):
                    if (result.get("source", {}).get("document_id") in fixture["document_ids"]
                            and result.get("output") == fixture["output"]):
                        return result.get("row_accounting")
        except (BlobNotFoundError, PackageResolutionError, ValueError, KeyError, AttributeError):
            return None
        return None

    def _fixture_source(self, plan: dict[str, Any], fixture: dict[str, Any]) -> tuple[bytes, str]:
        """The verified source bytes and extension the fixture was parsed from (the plan's sources)."""
        from pathlib import PurePosixPath
        matches = [item for item in plan["sources"] if item.get("document_id") in fixture.get("document_ids", [])]
        source = matches[0] if len(matches) == 1 else None
        if not isinstance(source, dict):
            raise PackageResolutionError("PACKAGE_SOURCE_INVALID")
        data = self.storage.download_source(source["blob_path"], self.max_source_bytes)
        if len(data) != source["size_bytes"] or hashlib.sha256(data).hexdigest() != str(source["sha256"]).lower():
            raise PackageResolutionError("PACKAGE_SOURCE_INVALID")
        return data, PurePosixPath(source["blob_path"]).suffix.lower()

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
        facts = facts_from_xlsx(data)
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
        from .canonical_format import PairCountError, canonical_columns, row_pair_count
        try:
            # 8 pairs (today's 25 columns) unless a row needs more (plan 8.12); never truncated.
            columns = canonical_columns(row_pair_count(rows))
        except PairCountError as exc:
            raise PackageResolutionError("MANUFACTURER_PAIR_LIMIT") from exc
        normalized = []
        for index, item in enumerate(rows, 1):
            row = {column: item.get(column) for column in columns}
            row["line_number"] = index
            normalized.append(row)
        stream = io.BytesIO()
        with pd.ExcelWriter(stream, engine="openpyxl") as writer:
            pd.DataFrame(normalized, columns=columns).to_excel(writer, index=False)
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
        if "caller_application" in approval:
            _ids(approval["caller_application"])
            identity["caller_application"] = approval["caller_application"]
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
        if (manifest.get("version") not in (MANIFEST_V1, MANIFEST_V2)
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
        attached: set[str] = set()
        paths: dict[str, dict[str, Any]] = {}
        seen_groups: set[str] = set()
        for result in results:
            if (not isinstance(result, dict)
                or not _RESULT_KEYS <= set(result) <= _RESULT_KEYS | {"structure", "subassembly_document_ids"}
                or ("structure" in result and manifest["version"] != MANIFEST_V2)
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
                    or result.get("subassembly_document_ids") != group.get("subassembly_document_ids")
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
            attached.update(result.get("subassembly_document_ids", []))
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
                if "structure" in result:
                    self._structure_bytes(result["structure"], f"{prefix}/objects/")
            except PackageResolutionError as exc:
                raise PackageResolutionError("RESOLVED_OUTPUT_CONFLICT") from exc
        excluded = {doc for item in plan["assignments"] if item["kind"] == "excluded"
                    for doc in item["document_ids"]}
        # Unit 6: files set aside at review, and files used under a line of another BOM (a shared file
        # may be used under several), are covered without a result of their own.
        excluded |= {doc for item in approval.get("review", {}).get("groups", [])
                     if isinstance(item, dict) and item.get("status") == "excluded"
                     for doc in item.get("document_ids", [])}
        if attached & (excluded | set(covered)):
            raise PackageResolutionError("RESOLVED_OUTPUT_CONFLICT")
        expected = [item["document_id"] for item in plan["sources"]
                    if item["document_id"] not in excluded and item["document_id"] not in attached]
        if (manifest["version"] == MANIFEST_V2) != any("structure" in item for item in results):
            raise PackageResolutionError("RESOLVED_OUTPUT_CONFLICT")
        # A045: a combined BOM may feed several results, each through its own proven subset.
        from .bom_split import split_documents
        shared = split_documents([review_groups[item["group_id"]] for item in results])
        if shared is None:
            raise PackageResolutionError("RESOLVED_OUTPUT_CONFLICT")
        unique = sorted(set(covered))
        if (results != sorted(results, key=lambda item: (item["document_ids"], item["group_id"]))
                or unique != sorted(expected)
                or len(covered) - len(unique) != sum(covered.count(doc) - 1 for doc in shared)
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


def _cell(value: Any) -> Any:
    """A parsed-workbook cell, with pandas' empty-cell NaN read as None."""
    return None if value is None or (isinstance(value, float) and value != value) else value


def _cell_text(value: Any) -> str:
    value = _cell(value)
    return "" if value is None else str(value).strip()


# Package-choice warnings shown to the person; any of them stops automatic acceptance.
PACKAGE_CHOICE_FINDING_CODES = frozenset({"CHOICE_UNCONFIRMED",
    "SURVEY_BATCH_FAILED", "SURVEY_CALL_LIMIT", "CHOICE_ASSEMBLY_FAILED", "SURVEY_FILES_UNCLASSIFIED",
    "SURVEY_BOM_NOT_MAPPED", "SURVEY_MAPPING_UNCONFIRMED"})


def package_file_rows(result: dict[str, Any]) -> list[dict[str, Any]]:
    """One row per surveyed file: type, requested-assembly link and the package-choice decision."""
    by_file = {file_id: item for item in result["documents"] for file_id in item["survey_file_ids"]}
    rows = []
    for record in result["survey"]["files"]:
        document = by_file.get(record["file_id"])
        link = record.get("requested_assembly")
        if document is None:
            decision, reason = "not_parsed", "not_staged" if record["role"] == "bom" else record["role"]
        elif document["decision"] == "excluded":
            decision, reason = "excluded", document["reason"]
        else:
            decision = "primary" if document["reason"] == "primary" else "process"
            reason = document["reason"]
        rows.append({"file_id": record["file_id"], "path": record["path"], "kind": record["kind"],
                     "role": record["role"],
                     "classification_status": record.get("classification_status", "classified"),
                     "requested_assembly_id": None if link is None else link["assembly_id"],
                     "link": "none" if link is None else link["link"],
                     "decision": decision, "reason": reason,
                     "document_id": None if document is None else document["document_id"]})
    return rows


def _link_reason(matched: dict[str, Any]) -> str:
    if matched["source"] == "path":
        return f"{matched['text']} found in the file or folder name"
    return f"{matched['text']} found in sheet {matched['cell']['sheet']}, row {matched['cell']['row']}"


def requested_links(groups: list[dict[str, Any]], result: dict[str, Any]) -> list[dict[str, Any]]:
    """Mark each ready group whose requested assembly is obvious (round 2, D3, Nick 2026-10-10).

    All four must hold: (1) every source document of the group has a confirmed survey link, all to the
    same requested assembly; (2) the part number read during parsing names no other requested assembly;
    (3) the parsed revision does not contradict the requested one; (4) no other ready group links to that
    assembly. Anything else leaves the binding to a person. Same core-number rule as the survey.
    """
    from .assembly_core import find_assembly, revision_contradicts
    requested = {item["assembly_id"]: item for item in result["requested_assemblies"]}
    documents = {item["document_id"]: item for item in result["documents"]}
    records = {item["file_id"]: item for item in result["survey"]["files"]}
    proposed: dict[int, dict[str, Any]] = {}
    for index, group in enumerate(groups):
        if group.get("status") != "ready":
            continue
        # A045: a group built from one requested assembly's own lines of a combined BOM names it.
        subset_ids = {(item.get("subset") or {}).get("assembly_id") for item in group["evidence"].get("fixtures", [])}
        if any(item is not None for item in subset_ids):
            if len(subset_ids) == 1 and next(iter(subset_ids)) in requested:
                assembly_id = next(iter(subset_ids))
                identity = group["evidence"]["assembly_identity"]
                if not revision_contradicts(requested[assembly_id]["revision"], identity.get("revision")):
                    proposed[index] = {"assembly_id": assembly_id, "matched_text": identity["name"],
                                       "reason": f"{identity['name']} heads its own lines in a combined BOM"}
            continue
        sources = [documents.get(document_id) for document_id in group["document_ids"]]
        if not sources or any(item is None or not item.get("requested_assembly_ids") for item in sources):
            continue
        links = {assembly_id for item in sources for assembly_id in item["requested_assembly_ids"]}
        if len(links) != 1:
            continue
        assembly_id = next(iter(links))
        identity = group["evidence"]["assembly_identity"]
        if any(find_assembly(other["part_number"], identity["name"]) is not None
               for key, other in requested.items() if key != assembly_id):
            continue
        if revision_contradicts(requested[assembly_id]["revision"], identity.get("revision")):
            continue
        matched = next((records[file_id]["requested_assembly"]["matched"]
                        for item in sources for file_id in item["survey_file_ids"]
                        if (records[file_id].get("requested_assembly") or {}).get("link") == "confirmed"
                        and records[file_id]["requested_assembly"]["assembly_id"] == assembly_id), None)
        if matched is None:
            continue
        proposed[index] = {"assembly_id": assembly_id, "matched_text": matched["text"],
                           "reason": _link_reason(matched)}
    counts: dict[str, int] = {}
    for link in proposed.values():
        counts[link["assembly_id"]] = counts.get(link["assembly_id"], 0) + 1
    return [{**group, "evidence": {**group["evidence"], "requested_link": proposed[index]}}
            if index in proposed and counts[proposed[index]["assembly_id"]] == 1 else group
            for index, group in enumerate(groups)]


def requested_identity(result: dict[str, Any], document_ids: list[str], rows: list[dict[str, Any]],
                       parsed: Any) -> Any:
    """The assembly a parsed BOM belongs to, read as a person would (round 5, Nick 2026-10-11).

    1. A parsed line whose part number names a requested assembly (core-number rule), e.g. the
       Level 0 line of the assembly itself: that line's part number and revision.
    2. Else the requested assembly every source document is code-confirmed to belong to (file
       name, folder or title cells, checked by the survey), when the parsed identity names none.
    None leaves today's behaviour: the parsed identity, or a person's mapping. A BOM whose lines or
    parsed identity name a different requested assembly than its link is never overridden.
    """
    from .assembly_core import find_assembly, revision_contradicts
    from .semantic import AssemblyIdentity
    requested = {item["assembly_id"]: item for item in result["requested_assemblies"]}
    documents = {item["document_id"]: item for item in result["documents"]}
    sources = [documents.get(document_id) for document_id in document_ids]
    links = {assembly_id for item in sources if item is not None
             for assembly_id in item.get("requested_assembly_ids", [])}
    lines: dict[str, Any] = {}
    for row in rows:
        number = str(row.get("item_number") or "").strip()
        for assembly_id, item in requested.items():
            if number and find_assembly(item["part_number"], number) is not None:
                lines.setdefault(assembly_id, AssemblyIdentity(number, str(row.get("revision") or "").strip()))
    if len(lines) == 1:
        assembly_id, identity = next(iter(lines.items()))
        if not links or links == {assembly_id}:
            return identity
        return None
    if lines:
        return None
    named = {assembly_id for assembly_id, item in requested.items()
             if parsed is not None and find_assembly(item["part_number"], parsed.name) is not None}
    confirmed = len(links) == 1 and all(item is not None and item.get("requested_assembly_ids") for item in sources)
    # Unit 6: a sub-assembly file in the parent's folder keeps its own identity, so review can put it
    # under the line that names it instead of merging it with the parent's BOM.
    if any(item is not None and item.get("reason") == "subassembly_of" for item in sources):
        return None
    if confirmed and not named:
        item = requested[next(iter(links))]
        # A clearly different parsed revision is a contradiction a person resolves.
        if parsed is not None and revision_contradicts(item["revision"], parsed.revision):
            return None
        return AssemblyIdentity(item["part_number"], item["revision"] or "")
    return None
