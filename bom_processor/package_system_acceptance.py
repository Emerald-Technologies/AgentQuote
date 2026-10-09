"""System BOM resolution authority, separate from human package approval.

Inputs: byte-verified parent review, exact quote assembly identities and checkpoint.
Output: immutable system acceptance; engineering qualification remains downstream.
Run python -m pytest tests/test_package_system_acceptance.py. No model calls.
"""
from __future__ import annotations

from datetime import datetime, timezone

from .core import BlobConflictError, _json_bytes
from .package_resolution import ParentPackageService, PackageResolutionError, _hash, _json

POLICY = "conflict-free-bom-resolution.v1"
VERSION = "package-system-acceptance.v1"
AUTHORITY = "agentquote-bom-qualifier"


def system_acceptance_identity(record: dict) -> dict:
    return {key: record.get(key) for key in (
        "version", "plan_id", "review_manifest_sha256", "review", "authority",
        "policy_version", "assembly_identities", "caller_application")}


class SystemPackageService(ParentPackageService):
    """No human actor or selections; transport must authenticate caller separately."""

    def _verify_plan_sources(self, plan_id: str) -> None:
        plan = self._plan(plan_id)
        sources = {item["document_id"]: item for item in plan["sources"]}
        for source in sources.values():
            import hashlib
            data = self.storage.download_source(source["blob_path"], self.max_source_bytes)
            if len(data) != source["size_bytes"] or hashlib.sha256(data).hexdigest() != source["sha256"]:
                raise PackageResolutionError("SYSTEM_SOURCE_BINDING_INVALID")
        for assignment in plan["assignments"]:
            if assignment["kind"] == "candidate_request":
                child = _json(self.storage, f"candidates/requests/{assignment['request_id']}.json")
                expected = [sources[key] for key in sorted(assignment["document_ids"])]
                if self._normalized_child_sources(child.get("source_evidence")) != expected:
                    raise PackageResolutionError("SYSTEM_CHILD_SOURCE_BINDING_INVALID")

    @staticmethod
    def _timestamp(record: dict) -> None:
        try:
            raw = record["approved_time"]
            if not isinstance(raw, str) or len(raw) > 40:
                raise ValueError
            value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError
        except (KeyError, ValueError, TypeError):
            raise PackageResolutionError("SYSTEM_ACCEPTANCE_TIME_INVALID") from None

    def accept_system(self, *, plan_id: str, review_manifest_sha256: str,
                      assembly_identities: list[dict], caller_application: str,
                      _persist: bool = True) -> dict:
        from .package_writer_gate import package_writer
        from .package_writer_gate import assert_plan_coordination
        if not _persist:
            return self._accept_system_locked(plan_id=plan_id,
                review_manifest_sha256=review_manifest_sha256,
                assembly_identities=assembly_identities, caller_application=caller_application,
                _persist=False)
        assert_plan_coordination(self.storage, self._plan(plan_id))
        with package_writer(self.storage, plan_id, "approval") as guard:
            return self._accept_system_locked(plan_id=plan_id,
                review_manifest_sha256=review_manifest_sha256,
                assembly_identities=assembly_identities, caller_application=caller_application,
            _persist=_persist, _guard=guard)

    def _accept_system_locked(self, *, plan_id: str, review_manifest_sha256: str,
                      assembly_identities: list[dict], caller_application: str,
                      _persist: bool = True, _guard=None) -> dict:
        if (not isinstance(caller_application, str) or not caller_application
                or len(caller_application) > 128
                or any(not (c.isalnum() or c in "._-") for c in caller_application)):
            if _guard is not None:
                _guard.reject(PackageResolutionError("SYSTEM_CALLER_INVALID"))
            raise PackageResolutionError("SYSTEM_CALLER_INVALID")
        if (not isinstance(assembly_identities, list) or not assembly_identities
                or len(assembly_identities) > 50):
            if _guard is not None:
                _guard.reject(PackageResolutionError("SYSTEM_ASSEMBLY_COVERAGE_INVALID"))
            raise PackageResolutionError("SYSTEM_ASSEMBLY_COVERAGE_INVALID")
        identities = []
        for item in assembly_identities:
            if (not isinstance(item, dict) or set(item) != {"name", "revision"}
                    or any(not isinstance(item[key], str) or len(item[key]) > 256
                           for key in ("name", "revision")) or not item["name"].strip()):
                if _guard is not None:
                    _guard.reject(PackageResolutionError("SYSTEM_ASSEMBLY_IDENTITY_INVALID"))
                raise PackageResolutionError("SYSTEM_ASSEMBLY_IDENTITY_INVALID")
            identities.append({"name": item["name"].strip().casefold(),
                               "revision": item["revision"].strip().casefold()})
        identities.sort(key=lambda item: (item["name"], item["revision"]))
        if len({_hash(item) for item in identities}) != len(identities):
            if _guard is not None:
                _guard.reject(PackageResolutionError("SYSTEM_ASSEMBLY_IDENTITY_AMBIGUOUS"))
            raise PackageResolutionError("SYSTEM_ASSEMBLY_IDENTITY_AMBIGUOUS")
        # Independently rebuild full review without mappings/conflict choices.
        self._verify_plan_sources(plan_id)
        review = self.review(plan_id)
        if (not review["approval_ready"] or not review["complete"]
                or review["manifest_sha256"] != review_manifest_sha256
                or review["assembly_mappings"] or review["conflict_dispositions"]):
            if _guard is not None:
                _guard.reject(PackageResolutionError("SYSTEM_REVIEW_NOT_QUALIFIED"))
            raise PackageResolutionError("SYSTEM_REVIEW_NOT_QUALIFIED")
        actual = []
        covered = []
        for group in review["groups"]:
            if group.get("status") == "excluded" and group.get("kind") == "excluded":
                # Set aside by a completed package choice that code cross-checked.
                covered.extend(group["document_ids"])
                continue
            evidence = group.get("evidence", {})
            if (group.get("status") != "ready" or group.get("mapping_keys")
                    or group.get("disposition_keys")
                    or evidence.get("reconciliation") not in {"equivalent", "merged", "primary_selected"}
                    or not isinstance(evidence.get("assembly_identity"), dict)
                    or not evidence.get("fixtures")):
                if _guard is not None:
                    _guard.reject(PackageResolutionError("SYSTEM_REVIEW_REQUIRES_EXCEPTION"))
                raise PackageResolutionError("SYSTEM_REVIEW_REQUIRES_EXCEPTION")
            actual.append(evidence["assembly_identity"])
            covered.extend(group["document_ids"])
        actual.sort(key=lambda item: (item["name"], item["revision"]))
        plan = self._plan(plan_id)
        if (actual != identities or len(covered) != len(set(covered))
                or sorted(covered) != sorted(item["document_id"] for item in plan["sources"])):
            if _guard is not None:
                _guard.reject(PackageResolutionError("SYSTEM_ASSEMBLY_COVERAGE_INVALID"))
            raise PackageResolutionError("SYSTEM_ASSEMBLY_COVERAGE_INVALID")
        body = {"version": VERSION, "plan_id": plan_id,
                "review_manifest_sha256": review_manifest_sha256, "review": review,
                "authority": AUTHORITY, "policy_version": POLICY,
                "assembly_identities": identities, "caller_application": caller_application}
        approval_id = _hash(body)
        now = self.clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        record = {**body, "approval_id": approval_id,
                  "approved_time": now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")}
        if not _persist:
            return record
        path = self._approval_path(plan_id, approval_id)
        if _guard is not None:
            _guard.write_attempted = True
        try:
            self.storage.upload_registry_immutable(path, _json_bytes(record), "application/json")
        except BlobConflictError as exc:
            prior = _json(self.storage, path)
            if system_acceptance_identity(prior) != body or prior.get("approval_id") != approval_id:
                raise PackageResolutionError("SYSTEM_ACCEPTANCE_CONFLICT") from exc
            self._timestamp(prior)
            record = prior
        return record

    def _bound_approval(self, plan_id: str, approval_id: str) -> dict:
        record = _json(self.storage, self._approval_path(plan_id, approval_id))
        if record.get("version") != VERSION:
            return super()._bound_approval(plan_id, approval_id)
        body = system_acceptance_identity(record)
        if (record.get("authority") != AUTHORITY or record.get("policy_version") != POLICY
                or record.get("plan_id") != plan_id or record.get("approval_id") != approval_id
                or _hash(body) != approval_id):
            raise PackageResolutionError("SYSTEM_ACCEPTANCE_BINDING_INVALID")
        if set(record) != set(body) | {"approval_id", "approved_time"}:
            raise PackageResolutionError("SYSTEM_ACCEPTANCE_BINDING_INVALID")
        self._timestamp(record)
        # Reuse eligibility without publication: status/manifest reads stay read-only.
        verified = self.accept_system(plan_id=plan_id,
            review_manifest_sha256=record["review_manifest_sha256"],
            assembly_identities=record["assembly_identities"],
            caller_application=record["caller_application"], _persist=False)
        if system_acceptance_identity(verified) != body or verified["approval_id"] != approval_id:
            raise PackageResolutionError("SYSTEM_ACCEPTANCE_BINDING_INVALID")
        return record