"""Authenticated deterministic BOM processing HTTP service.

Run as a separate Container App from the prompt-driven Responses service:
    python deterministic_api.py

Production requires Azure Container Apps Authentication (Easy Auth) in front of
this service. The service validates the forwarded principal against explicit
application and/or object ID allowlists before processing any request.
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import re
import threading
import uuid
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from bom_processor import (
    AzureBlobStorage,
    BlobConflictError,
    BlobNotFoundError,
    BomProcessor,
    ProcessingError,
    ProcessingRequest,
    RemoteBomProcessor,
    AzureJobStarter,
    AzureSasFactory,
    JobOrchestrator,
    BlobRegistryReader,
    CandidateRequiredError,
    AggregateCandidateRequiredError,
    AdminDecisionService,
    CandidateStatusService,
    CandidateAutomationService,
    AzureCandidateJobStarter,
    CandidateReviewError,
    CandidateReviewService,
    PackageResolutionError,
    ParentPackageService,
)
from bom_processor.core import CONTRACT_VERSION
from bom_processor.master_registry import RegistryError
from bom_processor.package_system_acceptance import SystemPackageService

log = logging.getLogger("agentquote.deterministic_api")
ROUTE = "/api/v1/bom/process"
DECISION_ROUTE = "/api/v1/parser-candidates/decision"
STATUS_ROUTE_PREFIX = "/api/v1/parser-candidates/requests/"
REVIEW_OUTPUT_ROUTE = re.compile(
    r"^/api/v1/parser-candidates/requests/([A-Za-z0-9][A-Za-z0-9._-]{0,127})/"
    r"candidates/([A-Za-z0-9][A-Za-z0-9._-]{0,127})/validations/"
    r"([A-Za-z0-9][A-Za-z0-9._-]{0,127})/output$")
REVIEW_FIXTURE_OUTPUT_ROUTE = re.compile(
    r"^/api/v1/parser-candidates/requests/([A-Za-z0-9][A-Za-z0-9._-]{0,127})/"
    r"candidates/([A-Za-z0-9][A-Za-z0-9._-]{0,127})/validations/"
    r"([A-Za-z0-9][A-Za-z0-9._-]{0,127})/fixtures/([0-9a-fA-F]{64})/output$")
PACKAGE_ROUTE = re.compile(
    r"^/api/v1/bom-packages/([A-Za-z0-9][A-Za-z0-9._-]{0,127})/(status|review|approve|approval|accept-system)$")
PACKAGE_RESOLUTION_ROUTE = re.compile(
    r"^/api/v1/bom-packages/([A-Za-z0-9][A-Za-z0-9._-]{0,127})/approvals/"
    r"([A-Za-z0-9][A-Za-z0-9._-]{0,127})/(resolve|resolve-system|status|manifest)$")
PACKAGE_OUTPUT_ROUTE = re.compile(
    r"^/api/v1/bom-packages/([A-Za-z0-9][A-Za-z0-9._-]{0,127})/approvals/"
    r"([A-Za-z0-9][A-Za-z0-9._-]{0,127})/groups/"
    r"([A-Za-z0-9][A-Za-z0-9._-]{0,127})/output$")
EVIDENCE_ROUTE = re.compile(r"^/api/v1/package-evidence/([0-9a-f]{64})/(submit|start|status)$")


@dataclass(frozen=True)
class ApiConfig:
    host: str
    port: int
    auth_mode: str
    allowed_app_ids: frozenset[str]
    allowed_object_ids: frozenset[str]
    max_body_bytes: int
    max_concurrent_jobs: int
    processor_options: dict[str, int]
    candidate_automation_enabled: bool = False

    @classmethod
    def from_env(cls) -> "ApiConfig":
        auth_mode = os.environ.get("BOM_API_AUTH_MODE", "easy_auth").strip().lower()
        if auth_mode not in {"easy_auth", "local_disabled"}:
            raise ValueError("BOM_API_AUTH_MODE must be easy_auth or local_disabled")
        allowed_app_ids = _id_set(os.environ.get("BOM_API_ALLOWED_APP_IDS", ""))
        allowed_object_ids = _id_set(os.environ.get("BOM_API_ALLOWED_OBJECT_IDS", ""))
        if auth_mode == "easy_auth" and not (allowed_app_ids or allowed_object_ids):
            raise ValueError("Easy Auth requires an explicit caller allowlist")
        return cls(
            host=os.environ.get("BOM_API_HOST", "0.0.0.0"),
            port=_positive_int("BOM_API_PORT", 8088),
            auth_mode=auth_mode,
            allowed_app_ids=allowed_app_ids,
            allowed_object_ids=allowed_object_ids,
            max_body_bytes=_positive_int("BOM_API_MAX_BODY_BYTES", 131_072),
            max_concurrent_jobs=_positive_int("BOM_API_MAX_CONCURRENT_JOBS", 2),
            processor_options={
                "timeout_seconds": _positive_int("BOM_PROCESS_TIMEOUT_SECONDS", 60),
                "max_source_bytes": _positive_int("BOM_MAX_SOURCE_BYTES", 50_000_000),
                "max_total_source_bytes": _positive_int("BOM_MAX_TOTAL_SOURCE_BYTES", 100_000_000),
                "max_parser_bytes": _positive_int("BOM_MAX_PARSER_BYTES", 1_000_000),
                "max_output_bytes": _positive_int("BOM_MAX_OUTPUT_BYTES", 50_000_000),
                "max_rows": _positive_int("BOM_MAX_ROWS", 100_000),
            },
            candidate_automation_enabled=os.environ.get("BOM_CANDIDATE_AUTOMATION_ENABLED", "false").strip().lower() == "true",
        )


def _positive_int(name: str, default: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _id_set(raw: str) -> frozenset[str]:
    return frozenset(value.strip().lower() for value in raw.split(",") if value.strip())


def _decode_principal(encoded: str) -> dict[str, Any]:
    try:
        padding = "=" * (-len(encoded) % 4)
        decoded = base64.b64decode(encoded + padding, validate=True)
        value = json.loads(decoded.decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PermissionError("invalid identity") from exc
    if not isinstance(value, dict) or not isinstance(value.get("claims"), list):
        raise PermissionError("invalid identity")
    return value


def _claim_values(principal: dict[str, Any], claim_names: set[str]) -> set[str]:
    values: set[str] = set()
    for claim in principal["claims"]:
        if not isinstance(claim, dict):
            continue
        claim_type = str(claim.get("typ", "")).lower()
        claim_value = claim.get("val")
        if claim_type in claim_names and isinstance(claim_value, str):
            values.add(claim_value.lower())
    return values


def authorize(headers: Any, config: ApiConfig) -> None:
    """Authorize an Easy Auth principal without logging identity headers."""
    if config.auth_mode == "local_disabled":
        return
    encoded = headers.get("X-MS-CLIENT-PRINCIPAL", "")
    if not encoded:
        raise PermissionError("missing identity")
    principal = _decode_principal(encoded)
    app_ids = _claim_values(principal, {
        "appid", "azp", "http://schemas.microsoft.com/identity/claims/appid",
    })
    object_ids = _claim_values(principal, {
        "oid", "http://schemas.microsoft.com/identity/claims/objectidentifier",
    })
    if not ((app_ids & config.allowed_app_ids) or (object_ids & config.allowed_object_ids)):
        raise LookupError("caller not allowed")


def authenticated_actor(headers: Any, config: ApiConfig) -> str:
    """Return the verified Easy Auth object ID; never accept a body actor."""
    authorize(headers, config)
    if config.auth_mode == "local_disabled":
        return "local-disabled"
    object_ids = _claim_values(_decode_principal(headers.get("X-MS-CLIENT-PRINCIPAL", "")), {
        "oid", "http://schemas.microsoft.com/identity/claims/objectidentifier",
    })
    allowed = sorted(object_ids & config.allowed_object_ids)
    if len(allowed) != 1 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", allowed[0]):
        raise LookupError("verified actor unavailable")
    return allowed[0]


class ProcessingApi:
    """HTTP-neutral request adapter for deterministic processing."""

    def __init__(self, config: ApiConfig, processor_factory: Callable[[], BomProcessor], automation_factory=None) -> None:
        self.config = config
        self.processor_factory = processor_factory
        self.automation_factory = automation_factory
        self._capacity = threading.BoundedSemaphore(config.max_concurrent_jobs)

    def handle(self, headers: Any, body: bytes) -> tuple[int, dict[str, Any]]:
        request_id = uuid.uuid4().hex
        try:
            authorize(headers, self.config)
        except PermissionError:
            return 401, _error("unauthenticated", "Authentication is required.", request_id)
        except LookupError:
            return 403, _error("forbidden", "Caller is not authorized.", request_id)
        if len(body) > self.config.max_body_bytes:
            return 413, _error("request_too_large", "Request body exceeds the limit.", request_id)
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return 400, _error("invalid_json", "Request body must be valid JSON.", request_id)
        try:
            request = ProcessingRequest.from_dict(value)
        except ProcessingError:
            return 422, _error("invalid_request", "Processing request is invalid.", request_id)
        if not self._capacity.acquire(blocking=False):
            return 429, _error("busy", "Processing capacity is currently full.", request_id)
        try:
            return 200, self.processor_factory().process(request)
        except BlobConflictError:
            return 409, _error("immutable_conflict", "Job output conflicts with existing data.", request_id)
        except BlobNotFoundError:
            return 404, _error("artifact_not_found", "A required source or parser was not found.", request_id)
        except CandidateRequiredError as exc:
            payload = {"version": CONTRACT_VERSION, "status": "candidate_required", "candidate": {
                "request_id": exc.request_id, "parser_key": exc.parser_key,
                "profile_hash": exc.profile_hash}}
            if self.automation_factory:
                try:
                    if self.automation_factory().start_author(exc.request_id) == "exhausted":
                        payload["automation_error"] = "author_attempts_exhausted"
                except Exception:
                    payload["automation_error"] = "author_start_failed"
            return 202, payload
        except AggregateCandidateRequiredError as exc:
            groups = [{"request_id": item.request_id, "parser_key": item.parser_key,
                       "profile_hash": item.profile_hash,
                       "document_ids": list(item.document_ids), "state": item.state}
                      for item in exc.candidates]
            if self.automation_factory:
                automation = self.automation_factory()
                for group, item in zip(groups, exc.candidates):
                    try:
                        outcome = automation.start_author(item.request_id)
                        if outcome == "exhausted":
                            group["automation_error"] = "author_attempts_exhausted"
                        elif isinstance(outcome, str) and outcome.startswith("budget_"):
                            group["automation_error"] = outcome
                    except Exception:
                        group["automation_error"] = "author_start_failed"
            return 202, {"version": CONTRACT_VERSION, "status": "candidates_required",
                         "job_id": request.job_id,
                         "request_fingerprint": request.fingerprint(), "candidates": groups,
                         **({"plan_id": exc.plan_id} if exc.plan_id else {})}
        except ProcessingError as exc:
            if str(exc).startswith("registry:PARSER_FORMAT_NOT_RECOGNIZED"):
                return 422, _error("parser_format_not_recognized", "No approved parser matches the BOM.", request_id)
            if str(exc).startswith("registry:PARSER_FORMAT_AMBIGUOUS"):
                return 422, _error("parser_format_ambiguous", "Multiple approved parsers match the BOM.", request_id)
            code, status = ("parser_timeout", 504) if str(exc) == "parser timed out" else ("processing_failed", 422)
            return status, _error(code, "BOM processing failed.", request_id)
        except Exception as exc:
            log.error(
                "Unexpected processing failure; request_id=%s exception_type=%s",
                request_id,
                type(exc).__name__,
            )
            return 500, _error("internal_error", "An internal error occurred.", request_id)
        finally:
            self._capacity.release()


class DecisionApi:
    """HTTP-neutral adapter for the trusted Quick Quote decision contract."""

    def __init__(self, config: ApiConfig,
                 service_factory: Callable[[], AdminDecisionService]) -> None:
        self.config = config
        self.service_factory = service_factory

    def handle(self, headers: Any, body: bytes) -> tuple[int, dict[str, Any]]:
        request_id = uuid.uuid4().hex
        try:
            authorize(headers, self.config)
        except PermissionError:
            return 401, _error("unauthenticated", "Authentication is required.", request_id)
        except LookupError:
            return 403, _error("forbidden", "Caller is not authorized.", request_id)
        if len(body) > self.config.max_body_bytes:
            return 413, _error("request_too_large", "Request body exceeds the limit.", request_id)
        try:
            value = json.loads(body.decode("utf-8"))
            request = _decision_request(value)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return 400, _error("invalid_json", "Request body must be valid JSON.", request_id)
        except ValueError:
            return 422, _error("invalid_request", "Decision request is invalid.", request_id)
        try:
            result = self.service_factory().decide(**request)
            response = {"decision_id": result["decision_id"],
                         "status": result["decision"],
                         "candidate_id": result["candidate_id"],
                         "validation_id": result["validation_id"]}
            if isinstance(result.get("review_manifest_sha256"), str):
                response["reviewManifestSha256"] = result["review_manifest_sha256"]
            if isinstance(result.get("parser"), dict):
                response["parser"] = result["parser"]
            return 200, response
        except RegistryError as exc:
            code = str(exc)
            status = 409 if code in {"DECISION_CONFLICT", "REGISTRY_CONFLICT"} else 422
            return status, _error("decision_conflict" if status == 409 else "invalid_decision",
                                  "Decision could not be recorded." if status == 409 else
                                  "Decision request is invalid.", request_id)
        except (BlobNotFoundError, BlobConflictError):
            return 409, _error("decision_unavailable", "Decision could not be recorded.", request_id)
        except Exception as exc:
            log.error("Unexpected decision failure; request_id=%s exception_type=%s",
                      request_id, type(exc).__name__)
            return 500, _error("internal_error", "An internal error occurred.", request_id)


class CandidateStatusApi:
    def __init__(self, config: ApiConfig, service_factory: Callable[[], CandidateStatusService]) -> None:
        self.config, self.service_factory = config, service_factory

    def handle(self, headers: Any, request_id: str) -> tuple[int, dict[str, Any]]:
        response_id = uuid.uuid4().hex
        try:
            authorize(headers, self.config)
            return 200, self.service_factory().get(request_id)
        except PermissionError:
            return 401, _error("unauthenticated", "Authentication is required.", response_id)
        except LookupError:
            return 403, _error("forbidden", "Caller is not authorized.", response_id)
        except BlobNotFoundError:
            return 404, _error("not_found", "Request was not found.", response_id)
        except Exception as exc:
            # Do not reveal artifact details or distinguish malformed records.
            if exc.__class__.__name__ == "CandidateStatusError":
                return 409, _error("status_unavailable", "Request status is unavailable.", response_id)
            log.error("Unexpected status failure; request_id=%s exception_type=%s", response_id, type(exc).__name__)
            return 500, _error("internal_error", "An internal error occurred.", response_id)


class CandidateReviewOutputApi:
    def __init__(self, config: ApiConfig,
                 service_factory: Callable[[], CandidateReviewService]) -> None:
        self.config, self.service_factory = config, service_factory

    def handle(self, headers: Any, request_id: str, candidate_id: str,
               validation_id: str) -> tuple[int, bytes | dict[str, Any]]:
        response_id = uuid.uuid4().hex
        try:
            authorize(headers, self.config)
            return 200, self.service_factory().get(request_id, candidate_id, validation_id)[1]
        except PermissionError:
            return 401, _error("unauthenticated", "Authentication is required.", response_id)
        except LookupError:
            return 403, _error("forbidden", "Caller is not authorized.", response_id)
        except (CandidateReviewError, BlobNotFoundError):
            return 409, _error("review_unavailable", "Parsed BOM review is unavailable.", response_id)

    def handle_fixture(self, headers: Any, request_id: str, candidate_id: str,
                       validation_id: str, fixture_sha256: str) -> tuple[int, bytes | dict[str, Any]]:
        response_id = uuid.uuid4().hex
        try:
            authorize(headers, self.config)
            service = self.service_factory()
            evidence = service.get_v3(request_id, candidate_id, validation_id)
            for group in evidence["groups"]:
                for fixture in group["fixtures"]:
                    if fixture.get("fixture_sha256", "").lower() == fixture_sha256.lower():
                        output = fixture.get("output", {})
                        data = service.storage.download_candidate(output.get("blob_path", ""), 50_000_000)
                        import hashlib
                        if (len(data) != output.get("size_bytes")
                                or hashlib.sha256(data).hexdigest() != output.get("sha256")
                                or output.get("content_type") != "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"):
                            raise CandidateReviewError("REVIEW_OUTPUT_INVALID")
                        return 200, data
            raise CandidateReviewError("REVIEW_FIXTURE_NOT_FOUND")
        except PermissionError:
            return 401, _error("unauthenticated", "Authentication is required.", response_id)
        except LookupError:
            return 403, _error("forbidden", "Caller is not authorized.", response_id)
        except (CandidateReviewError, BlobNotFoundError):
            return 409, _error("review_unavailable", "Parsed BOM review is unavailable.", response_id)


class PackageApi:
    """Authenticated adapter for additive package status, approval, and output routes."""
    def __init__(self, config: ApiConfig, service_factory: Callable[[], ParentPackageService]) -> None:
        self.config, self.service_factory = config, service_factory

    def handle_get(self, headers: Any, plan_id: str, action: str,
                   approval_id: str | None = None) -> tuple[int, dict[str, Any]]:
        response_id = uuid.uuid4().hex
        try:
            authorize(headers, self.config)
            service = self.service_factory()
            if action in {"status", "review"} and approval_id is None:
                return 200, service.review(plan_id)
            if action == "approval" and approval_id is None:
                return 200, service.approval(plan_id)
            if action == "status" and approval_id is not None:
                return 200, service.resolution_status(plan_id, approval_id)
            if action == "manifest" and approval_id is not None:
                return 200, service.manifest(plan_id, approval_id)
            return 405, _error("method_not_allowed", "Method is not allowed.", response_id)
        except PermissionError:
            return 401, _error("unauthenticated", "Authentication is required.", response_id)
        except LookupError:
            return 403, _error("forbidden", "Caller is not authorized.", response_id)
        except (PackageResolutionError, BlobNotFoundError):
            return 409, _error("package_unavailable", "Package state is unavailable.", response_id)
        except Exception as exc:
            log.error("Unexpected package GET failure; request_id=%s exception_type=%s",
                      response_id, type(exc).__name__)
            return 500, _error("internal_error", "An internal error occurred.", response_id)

    def handle_output(self, headers: Any, plan_id: str, approval_id: str,
                      group_id: str) -> tuple[int, bytes | dict[str, Any]]:
        response_id = uuid.uuid4().hex
        try:
            authorize(headers, self.config)
            return 200, self.service_factory().resolved_workbook(plan_id, approval_id, group_id)
        except PermissionError:
            return 401, _error("unauthenticated", "Authentication is required.", response_id)
        except LookupError:
            return 403, _error("forbidden", "Caller is not authorized.", response_id)
        except (PackageResolutionError, BlobNotFoundError):
            return 409, _error("package_unavailable", "Package output is unavailable.", response_id)
        except Exception as exc:
            log.error("Unexpected package output failure; request_id=%s exception_type=%s",
                      response_id, type(exc).__name__)
            return 500, _error("internal_error", "An internal error occurred.", response_id)

    def handle_post(self, headers: Any, plan_id: str, action: str, body: bytes,
                    approval_id: str | None = None) -> tuple[int, dict[str, Any]]:
        response_id = uuid.uuid4().hex
        try:
            if action in {"accept-system", "resolve-system"}:
                authorize(headers, self.config)
                if os.environ.get("PACKAGE_SYSTEM_ACCEPTANCE_ENABLED") != "true":
                    return 403, _error("disabled", "System acceptance is disabled.", response_id)
                # Require explicitly allowed application identity, not a human
                # object-ID authorization substituted for system authority.
                principal = _decode_principal(headers.get("X-MS-CLIENT-PRINCIPAL", ""))
                applications = _claim_values(principal, {
                    "appid", "azp", "http://schemas.microsoft.com/identity/claims/appid",
                }) & self.config.allowed_app_ids
                if len(applications) != 1:
                    raise LookupError("system application required")
                value = json.loads(body.decode("utf-8"))
                service = self.service_factory()
                if not isinstance(service, SystemPackageService):
                    raise PackageResolutionError("SYSTEM_SERVICE_UNAVAILABLE")
                if action == "resolve-system":
                    if value != {} or approval_id is None:
                        raise ValueError
                    bound = service._bound_approval(plan_id, approval_id)
                    if (bound.get("version") != "package-system-acceptance.v1"
                            or bound.get("caller_application") != next(iter(applications))):
                        raise LookupError("system caller mismatch")
                    return 200, service.resolve(plan_id=plan_id, approval_id=approval_id)
                if (not isinstance(value, dict)
                        or set(value) != {"review_manifest_sha256", "assembly_identities"}):
                    raise ValueError
                return 200, service.accept_system(plan_id=plan_id,
                    caller_application=next(iter(applications)), **value)
            authorize(headers, self.config)
            value = json.loads(body.decode("utf-8"))
            caller_application = None
            if isinstance(value, dict) and set(value) == {"operator_id", "approval"}:
                principal = _decode_principal(headers.get("X-MS-CLIENT-PRINCIPAL", ""))
                if (_claim_values(principal, {"idtyp"}) != {"app"}
                        or _claim_values(principal, {"scp", "http://schemas.microsoft.com/identity/claims/scope"})):
                    raise LookupError("application-only identity required")
                applications = _claim_values(principal, {
                    "appid", "azp", "http://schemas.microsoft.com/identity/claims/appid",
                }) & self.config.allowed_app_ids
                if len(applications) != 1:
                    raise LookupError("trusted application required")
                actor = value["operator_id"]
                if not isinstance(actor, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", actor):
                    raise ValueError
                caller_application = next(iter(applications))
                value = value["approval"]
            else:
                actor = authenticated_actor(headers, self.config)
            if not isinstance(value, dict) or any(key in value for key in ("actor", "actor_id", "approver")):
                raise ValueError
            service = self.service_factory()
            if action == "approve" and approval_id is None:
                allowed = {"review_manifest_sha256", "assembly_mappings", "conflict_dispositions", "reason", "ticket"}
                if set(value) - allowed or not isinstance(value.get("review_manifest_sha256"), str):
                    raise ValueError
                return 200, service.approve(plan_id=plan_id, approver=actor,
                    **({"caller_application": caller_application} if caller_application is not None else {}), **value)
            if action == "resolve" and approval_id is not None and not value:
                bound = service._bound_approval(plan_id, approval_id)
                if bound.get("version") == "package-system-acceptance.v1":
                    raise LookupError("system resolution route required")
                if caller_application is not None:
                    if bound.get("caller_application") != caller_application or bound.get("approver") != actor:
                        raise LookupError("delegated approval caller mismatch")
                elif "caller_application" in bound:
                    raise LookupError("delegated resolution route required")
                return 200, service.resolve(plan_id=plan_id, approval_id=approval_id)
            raise ValueError
        except PermissionError:
            return 401, _error("unauthenticated", "Authentication is required.", response_id)
        except LookupError:
            return 403, _error("forbidden", "Caller is not authorized.", response_id)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return 400, _error("invalid_json", "Request body must be valid JSON.", response_id)
        except ValueError:
            return 422, _error("invalid_request", "Package request is invalid.", response_id)
        except (PackageResolutionError, BlobNotFoundError, BlobConflictError):
            return 409, _error("package_unavailable", "Package operation could not be completed.", response_id)
        except Exception as exc:
            log.error("Unexpected review output failure; request_id=%s exception_type=%s",
                      response_id, type(exc).__name__)
            return 500, _error("internal_error", "An internal error occurred.", response_id)

def _decision_request(value: Any) -> dict[str, Any]:
    """Bind both deployed camelCase and service snake_case names, strictly."""
    if not isinstance(value, dict):
        raise ValueError("object required")
    aliases = {
        "request_id": "request_id", "requestId": "request_id",
        "candidate_id": "candidate_id", "candidateId": "candidate_id",
        "validation_id": "validation_id", "validationId": "validation_id",
        "decision": "decision", "variant_id": "variant_id", "variantId": "variant_id",
        "version": "version", "reason": "reason", "ticket": "ticket",
        "actor_id": "actor_id", "actorId": "actor_id",
        "idempotency_key": "idempotency_key", "idempotencyKey": "idempotency_key",
        "review_manifest_sha256": "review_manifest_sha256", "reviewManifestSha256": "review_manifest_sha256",
        "fixture_selections": "fixture_selections", "fixtureSelections": "fixture_selections",
    }
    if any(key not in aliases for key in value) or len({aliases[key] for key in value}) != len(value):
        raise ValueError("unknown or duplicate field")
    bound = {aliases[key]: item for key, item in value.items()}
    required = {"request_id", "candidate_id", "validation_id", "decision", "actor_id"}
    if not required.issubset(bound) or not all(isinstance(bound[key], str) for key in required):
        raise ValueError("required field missing")
    if any(key in bound and not isinstance(bound[key], str) for key in ("reason", "ticket", "review_manifest_sha256")):
        raise ValueError("invalid audit field")
    if "fixture_selections" in bound and (not isinstance(bound["fixture_selections"], dict)
            or any(not isinstance(key, str) or not isinstance(item, str)
                   for key, item in bound["fixture_selections"].items())):
        raise ValueError("invalid fixture selections")
    bound.setdefault("reason", "")
    bound.setdefault("ticket", "")
    if bound["decision"] not in {"approve", "reject"}:
        raise ValueError("invalid decision")
    if bound["decision"] == "approve":
        if not all(isinstance(bound.get(key), str) for key in ("variant_id", "version")):
            raise ValueError("approval variant missing")
    elif "variant_id" in bound or "version" in bound or "fixture_selections" in bound:
        raise ValueError("rejection variant supplied")
    actor = bound["actor_id"]
    if not actor or len(actor) > 128 or not all(char.isalnum() or char in "._-" for char in actor):
        raise ValueError("invalid actor")
    if "idempotency_key" in bound and (
        not isinstance(bound["idempotency_key"], str) or not bound["idempotency_key"] or
        len(bound["idempotency_key"] ) > 128 or
        not all(char.isalnum() or char in "._-" for char in bound["idempotency_key"])
    ):
        raise ValueError("invalid idempotency key")
    bound["approver"] = {"object_id": actor}
    bound.pop("actor_id")
    bound.pop("idempotency_key", None)
    return bound


def _error(code: str, message: str, request_id: str) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "request_id": request_id}}


def _storage_factory() -> AzureBlobStorage:
    account_url = os.environ.get("BOM_STORAGE_ACCOUNT_URL", "").strip()
    if account_url:
        from azure.identity import DefaultAzureCredential, ManagedIdentityCredential
        client_id = os.environ.get("BOM_STORAGE_MANAGED_IDENTITY_CLIENT_ID", "").strip()
        credential = (
            ManagedIdentityCredential(client_id=client_id)
            if client_id
            else DefaultAzureCredential()
        )
        return AzureBlobStorage.from_account_url(account_url, credential)
    else:
        connection_string = os.environ.get("BOM_STORAGE_CONNECTION_STRING", "")
        if not connection_string:
            raise RuntimeError("BOM storage configuration is not set")
        return AzureBlobStorage(connection_string)


def _processor_factory(config: ApiConfig) -> BomProcessor:
    storage = _storage_factory()
    account_url = os.environ.get("BOM_STORAGE_ACCOUNT_URL", "").strip()
    if account_url:
        from azure.identity import DefaultAzureCredential, ManagedIdentityCredential
        client_id = os.environ.get("BOM_STORAGE_MANAGED_IDENTITY_CLIENT_ID", "").strip()
        credential = (ManagedIdentityCredential(client_id=client_id) if client_id
                      else DefaultAzureCredential())
    if os.environ.get("BOM_PROCESSOR_MODE", "local").strip().lower() != "remote":
        return BomProcessor(storage, **config.processor_options)
    required = ("BOM_JOB_SUBSCRIPTION_ID", "BOM_JOB_RESOURCE_GROUP", "BOM_JOB_NAME")
    if any(not os.environ.get(name, "").strip() for name in required):
        raise RuntimeError("remote processor job configuration is not set")
    # The storage adapter intentionally reuses the validated source/parser
    # evidence methods rather than creating a second Azure client.
    class Reader:
        def read(self, container: str, blob: str) -> bytes:
            if container == "boms":
                return storage.download_source(blob, config.processor_options["max_source_bytes"])
            if container == "parsers":
                return storage.download_registry(blob, config.processor_options["max_parser_bytes"])
            raise ProcessingError("job storage container invalid")
    sas = AzureSasFactory(account_url, credential)
    starter = AzureJobStarter(
        subscription_id=os.environ["BOM_JOB_SUBSCRIPTION_ID"],
        resource_group=os.environ["BOM_JOB_RESOURCE_GROUP"],
        job_name=os.environ["BOM_JOB_NAME"], credential=credential,
        container_name=os.environ.get("BOM_JOB_CONTAINER_NAME", "synthetic-parser-worker"),
        template={
            "image": os.environ.get("BOM_WORKER_IMAGE", ""),
            "resources": {
                "cpu": float(os.environ.get("BOM_WORKER_CPU", "0.5")),
                "memory": os.environ.get("BOM_WORKER_MEMORY", "1Gi"),
            },
        },
    )
    budget_policy = os.environ.get("BOM_AUTHOR_GROUP_BUDGET", "false")
    if budget_policy not in {"true", "false"}:
        raise RuntimeError("invalid author budget policy")
    return RemoteBomProcessor(storage, JobOrchestrator(Reader(), sas, starter),
                               author_budget_enabled=budget_policy == "true",
                               author_budget_quote_ids=_author_budget_quote_ids(),
                               registry_reader=BlobRegistryReader(storage.download_registry,
                                                                   max_bytes=config.processor_options["max_parser_bytes"]),
                               **config.processor_options)


def _candidate_automation_factory(config: ApiConfig, storage: Any):
    if not config.candidate_automation_enabled:
        return None
    required = ("BOM_JOB_SUBSCRIPTION_ID", "BOM_JOB_RESOURCE_GROUP",
                "BOM_CANDIDATE_AUTHOR_JOB_NAME", "BOM_CANDIDATE_VALIDATOR_JOB_NAME")
    if any(not os.environ.get(name, "").strip() for name in required):
        raise RuntimeError("candidate automation configuration is not set")
    association_policy = os.environ.get("BOM_SOURCE_ASSOCIATIONS", "false")
    if association_policy not in {"true", "false"}:
        raise RuntimeError("invalid association policy")
    from azure.identity import DefaultAzureCredential, ManagedIdentityCredential
    client_id = os.environ.get("BOM_JOB_MANAGED_IDENTITY_CLIENT_ID", "").strip()
    credential = ManagedIdentityCredential(client_id=client_id) if client_id else DefaultAzureCredential()
    common = {"subscription_id": os.environ["BOM_JOB_SUBSCRIPTION_ID"],
              "resource_group": os.environ["BOM_JOB_RESOURCE_GROUP"], "credential": credential}
    resources = {"cpu": float(os.environ.get("BOM_CANDIDATE_CPU", "0.5")),
                 "memory": os.environ.get("BOM_CANDIDATE_MEMORY", "1Gi")}
    author_environment = {
        "AZURE_CLIENT_ID": os.environ.get("BOM_CANDIDATE_IDENTITY_CLIENT_ID", ""),
        "BOM_STORAGE_ACCOUNT_URL": os.environ.get("BOM_STORAGE_ACCOUNT_URL", ""),
        "FOUNDRY_PROJECT_ENDPOINT": os.environ.get("BOM_CANDIDATE_FOUNDRY_PROJECT_ENDPOINT", ""),
        "AZURE_AI_MODEL_DEPLOYMENT_NAME_FLASH": os.environ.get("BOM_CANDIDATE_MODEL_DEPLOYMENT", "gpt-5.6-luna"),
    }
    if association_policy == "true":
        author_environment["BOM_SOURCE_ASSOCIATIONS"] = "true"
    validator_environment = {
        "AZURE_CLIENT_ID": os.environ.get("BOM_CANDIDATE_IDENTITY_CLIENT_ID", ""),
        "BOM_STORAGE_ACCOUNT_URL": os.environ.get("BOM_STORAGE_ACCOUNT_URL", ""),
        "BOM_JOB_SUBSCRIPTION_ID": os.environ["BOM_JOB_SUBSCRIPTION_ID"],
        "BOM_JOB_RESOURCE_GROUP": os.environ["BOM_JOB_RESOURCE_GROUP"],
        "BOM_JOB_NAME": os.environ.get("BOM_JOB_NAME", ""),
        "BOM_JOB_CONTAINER_NAME": os.environ.get("BOM_JOB_CONTAINER_NAME", "synthetic-parser-worker"),
        "BOM_WORKER_IMAGE": os.environ.get("BOM_WORKER_IMAGE", ""),
        "BOM_PROCESS_TIMEOUT_SECONDS": os.environ.get("BOM_CANDIDATE_VALIDATION_TIMEOUT_SECONDS", "600"),
        "BOM_POLL_INTERVAL_SECONDS": os.environ.get("BOM_CANDIDATE_POLL_INTERVAL_SECONDS", "2"),
    }
    if any(not value for value in author_environment.values()) or any(not value for value in validator_environment.values()):
        raise RuntimeError("candidate automation environment is not set")
    max_author_attempts: int | None = None
    raw_limit = os.environ.get("BOM_CANDIDATE_MAX_AUTHOR_ATTEMPTS", "").strip()
    if raw_limit:
        max_author_attempts = int(raw_limit)
    return CandidateAutomationService(
        storage,
        AzureCandidateJobStarter(**common, job_name=os.environ["BOM_CANDIDATE_AUTHOR_JOB_NAME"],
                                 container_name=os.environ.get("BOM_CANDIDATE_AUTHOR_CONTAINER_NAME", "candidate-author"),
                                 image=os.environ.get("BOM_CANDIDATE_AUTHOR_IMAGE", ""),
                                 base_environment=author_environment, resources=resources),
        AzureCandidateJobStarter(**common, job_name=os.environ["BOM_CANDIDATE_VALIDATOR_JOB_NAME"],
                                 container_name=os.environ.get("BOM_CANDIDATE_VALIDATOR_CONTAINER_NAME", "candidate-validator"),
                                 image=os.environ.get("BOM_CANDIDATE_VALIDATOR_IMAGE", ""),
                                 base_environment=validator_environment, resources=resources),
        max_author_attempts=max_author_attempts,
        association_quote_ids=_association_quote_ids(),
        author_budget_enabled=_author_budget_enabled() or bool(_author_budget_quote_ids()))


def _author_budget_quote_ids() -> frozenset[str]:
    from bom_processor.quote_budget_scope import parse_quote_scope
    return parse_quote_scope(os.environ.get("BOM_AUTHOR_GROUP_BUDGET_QUOTE_UUIDS", ""))


def _association_quote_ids() -> frozenset[str]:
    from bom_processor.quote_budget_scope import parse_quote_scope
    scope = parse_quote_scope(os.environ.get("BOM_SOURCE_ASSOCIATIONS_QUOTE_UUIDS", ""))
    policy = os.environ.get("BOM_SOURCE_ASSOCIATIONS", "false")
    if policy not in {"true", "false"}:
        raise RuntimeError("invalid association policy")
    if scope and not _author_budget_enabled() and not scope <= _author_budget_quote_ids():
        raise RuntimeError("association quote scope requires budget enrollment")
    return scope


def _author_budget_enabled() -> bool:
    value = os.environ.get("BOM_AUTHOR_GROUP_BUDGET", "false")
    if value not in {"true", "false"}:
        raise RuntimeError("invalid author budget policy")
    return value == "true"


def make_handler(api: ProcessingApi, decision_api: DecisionApi | None = None,
                 status_api: CandidateStatusApi | None = None,
                 review_output_api: CandidateReviewOutputApi | None = None,
                 package_api: PackageApi | None = None,
                 evidence_api=None, recovery_api=None) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "AgentQuoteDeterministicAPI/1"

        def do_POST(self) -> None:  # noqa: N802
            if self.path in {"/api/v1/parser-author-recoveries", "/api/v1/parser-author-recoveries/lookup"} and recovery_api is not None:
                try:
                    recovery_api.authenticate(self.headers)
                except PermissionError:
                    self._write(401, {"error": {"code": "unauthenticated"}}); return
                except LookupError:
                    self._write(403, {"error": {"code": "forbidden"}}); return
                raw = self.headers.get("Content-Length", "")
                from bom_processor.author_recovery_api import MAX_BODY
                if not raw.isdigit() or int(raw) > MAX_BODY:
                    self._write(413, {"error": {"code": "request_too_large"}}); return
                try:
                    from bom_processor.package_evidence_api import read_evidence_body
                    body = read_evidence_body(self.rfile, self.connection, int(raw))
                except (OSError, ValueError):
                    self.close_connection = True
                    self._write(408, {"error": {"code": "body_timeout"}}); return
                action = recovery_api.lookup if self.path.endswith("/lookup") else recovery_api.handle
                status, payload = action(self.headers, body)
                self._write(status, payload); return
            system_match = PACKAGE_ROUTE.fullmatch(self.path)
            system_resolution = PACKAGE_RESOLUTION_ROUTE.fullmatch(self.path)
            if package_api is not None and ((system_match and system_match.group(2) == "accept-system")
                    or (system_resolution and system_resolution.group(3) == "resolve-system")):
                try:
                    authorize(self.headers, api.config)
                except PermissionError:
                    self._write(401, _error("unauthenticated", "Authentication is required.", uuid.uuid4().hex)); return
                except LookupError:
                    self._write(403, _error("forbidden", "Caller is not authorized.", uuid.uuid4().hex)); return
                raw = self.headers.get("Content-Length", "")
                if not raw.isdigit() or int(raw) > 65536:
                    self._write(413, _error("request_too_large", "Invalid system acceptance body length.", uuid.uuid4().hex)); return
                try:
                    from bom_processor.package_evidence_api import read_evidence_body
                    body = read_evidence_body(self.rfile, self.connection, int(raw))
                except (OSError, ValueError):
                    self.close_connection = True
                    self._write(408, _error("body_timeout", "Request body could not be read.", uuid.uuid4().hex)); return
                if system_match:
                    status, payload = package_api.handle_post(self.headers, system_match.group(1), "accept-system", body)
                else:
                    status, payload = package_api.handle_post(self.headers, system_resolution.group(1),
                        "resolve-system", body, system_resolution.group(2))
                self._write(status, payload); return
            evidence_match = EVIDENCE_ROUTE.fullmatch(self.path)
            if evidence_match and evidence_match.group(2) in {"submit", "start"} and evidence_api is not None:
                # Authenticate before reading evidence payloads or creating services.
                try:
                    authorize(self.headers, api.config)
                except PermissionError:
                    self._write(401, _error("unauthenticated", "Authentication is required.", uuid.uuid4().hex)); return
                except LookupError:
                    self._write(403, _error("forbidden", "Caller is not authorized.", uuid.uuid4().hex)); return
                from bom_processor.package_evidence_jobs import MAX_METADATA
                raw = self.headers.get("Content-Length", "")
                if not raw.isdigit() or int(raw) > MAX_METADATA:
                    self._write(413, _error("request_too_large", "Invalid evidence body length.", uuid.uuid4().hex)); return
                try:
                    from bom_processor.package_evidence_api import read_evidence_body
                    body = read_evidence_body(self.rfile, self.connection, int(raw))
                except (OSError, ValueError):
                    self.close_connection = True
                    self._write(408, _error("body_timeout", "Evidence body could not be read.", uuid.uuid4().hex)); return
                status, payload = evidence_api.handle(self.headers, *evidence_match.groups(), body)
                self._write(status, payload); return
            raw_length = self.headers.get("Content-Length")
            try:
                length = int(raw_length) if raw_length is not None else -1
            except ValueError:
                length = -1
            if length < 0:
                self._write(411, _error("length_required", "Content-Length is required.", uuid.uuid4().hex))
                return
            if length > api.config.max_body_bytes:
                self._write(413, _error("request_too_large", "Request body exceeds the limit.", uuid.uuid4().hex))
                return
            # Consume bounded POST bodies even for unknown routes. Returning on
            # Windows with unread bytes can reset the connection before the
            # client receives the intended HTTP error response.
            body = self.rfile.read(length)
            package_match = PACKAGE_ROUTE.fullmatch(self.path)
            resolution_match = PACKAGE_RESOLUTION_ROUTE.fullmatch(self.path)
            if package_match and package_match.group(2) == "approve" and package_api is not None:
                status, payload = package_api.handle_post(self.headers, package_match.group(1), "approve", body)
                self._write(status, payload)
                return
            if resolution_match and resolution_match.group(3) == "resolve" and package_api is not None:
                status, payload = package_api.handle_post(
                    self.headers, resolution_match.group(1), "resolve", body, resolution_match.group(2))
                self._write(status, payload)
                return
            if self.path not in {ROUTE, DECISION_ROUTE}:
                self._write(404, _error("not_found", "Route was not found.", uuid.uuid4().hex))
                return
            target = decision_api if self.path == DECISION_ROUTE else api
            if target is None:
                self._write(404, _error("not_found", "Route was not found.", uuid.uuid4().hex))
                return
            status, payload = target.handle(self.headers, body)
            self._write(status, payload)

        def do_GET(self) -> None:  # noqa: N802
            evidence_match = EVIDENCE_ROUTE.fullmatch(self.path)
            if evidence_match and evidence_match.group(2) == "status" and evidence_api is not None:
                status, payload = evidence_api.handle(self.headers, *evidence_match.groups())
                self._write(status, payload); return
            package_match = PACKAGE_ROUTE.fullmatch(self.path)
            if package_match and package_match.group(2) in {"status", "review", "approval"} and package_api is not None:
                status, payload = package_api.handle_get(self.headers, *package_match.groups())
                self._write(status, payload)
                return
            output_match = PACKAGE_OUTPUT_ROUTE.fullmatch(self.path)
            if output_match and package_api is not None:
                status, payload = package_api.handle_output(self.headers, *output_match.groups())
                self._write_binary(status, payload) if isinstance(payload, bytes) else self._write(status, payload)
                return
            resolution_match = PACKAGE_RESOLUTION_ROUTE.fullmatch(self.path)
            if resolution_match and resolution_match.group(3) in {"status", "manifest"} and package_api is not None:
                status, payload = package_api.handle_get(
                    self.headers, resolution_match.group(1), resolution_match.group(3), resolution_match.group(2))
                self._write(status, payload)
                return
            fixture_match = REVIEW_FIXTURE_OUTPUT_ROUTE.fullmatch(self.path)
            if fixture_match:
                if review_output_api is None:
                    self._write(404, _error("not_found", "Route was not found.", uuid.uuid4().hex))
                    return
                response_status, payload = review_output_api.handle_fixture(self.headers, *fixture_match.groups())
                self._write_binary(response_status, payload) if isinstance(payload, bytes) else self._write(response_status, payload)
                return
            review_match = REVIEW_OUTPUT_ROUTE.fullmatch(self.path)
            if review_match:
                if review_output_api is None:
                    self._write(404, _error("not_found", "Route was not found.", uuid.uuid4().hex))
                    return
                response_status, payload = review_output_api.handle(self.headers, *review_match.groups())
                if isinstance(payload, bytes):
                    self._write_binary(response_status, payload)
                else:
                    self._write(response_status, payload)
                return
            if self.path.startswith(STATUS_ROUTE_PREFIX):
                request_id = self.path[len(STATUS_ROUTE_PREFIX):]
                if status_api is None or not request_id or "/" in request_id or "?" in request_id:
                    self._write(404, _error("not_found", "Route was not found.", uuid.uuid4().hex))
                    return
                response_status, payload = status_api.handle(self.headers, request_id)
                self._write(response_status, payload)
                return
            status = 405 if self.path in {ROUTE, DECISION_ROUTE} else 404
            self._write(status, _error("method_not_allowed" if status == 405 else "not_found",
                                       "Method is not allowed." if status == 405 else "Route was not found.",
                                       uuid.uuid4().hex))

        def log_message(self, format_string: str, *args: Any) -> None:
            log.info("HTTP request completed: %s", format_string % args)

        def _write(self, status: int, payload: dict[str, Any]) -> None:
            data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)

        def _write_binary(self, status: int, data: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            self.send_header("Content-Disposition", 'attachment; filename="parsed-bom.xlsx"')
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)

    return Handler


def main() -> None:
    # Reject both selectors before clients, routes or the listening server exist.
    _author_budget_enabled()
    _author_budget_quote_ids()
    _association_quote_ids()
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    config = ApiConfig.from_env()
    api = ProcessingApi(config, lambda: _processor_factory(config),
                        (lambda: _candidate_automation_factory(config, _storage_factory()))
                        if config.candidate_automation_enabled else None)
    decision_api = DecisionApi(config, lambda: AdminDecisionService(_storage_factory()))
    status_api = CandidateStatusApi(
        config,
        lambda: CandidateStatusService(
            (storage := _storage_factory()),
            automation=(_candidate_automation_factory(config, storage)
                        if config.candidate_automation_enabled else None)))
    review_output_api = CandidateReviewOutputApi(
        config, lambda: CandidateReviewService(_storage_factory()))
    package_api = PackageApi(config, lambda: SystemPackageService(_storage_factory()))
    from bom_processor.author_recovery import RecoveryService
    from bom_processor.author_recovery_api import RecoveryApi
    recovery_setting = os.environ.get("BOM_AUTHOR_RECOVERY_ENABLED", "false")
    if recovery_setting not in {"true", "false"}:
        raise RuntimeError("invalid author recovery policy")
    def recovery_service():
        storage = _storage_factory()
        automation = _candidate_automation_factory(config, storage)
        if automation is None:
            raise RuntimeError("author recovery automation unavailable")
        return RecoveryService(storage, automation)
    recovery_api = RecoveryApi(config, recovery_service, enabled=recovery_setting == "true")
    evidence_api = None
    if os.environ.get("PACKAGE_EVIDENCE_API_ENABLED") == "true":
        from bom_processor.package_evidence_api import EvidenceApi, configured_policy
        from bom_processor.package_evidence_jobs import EvidenceJobService
        def evidence_service():
            from azure.identity import ManagedIdentityCredential
            required = ("BOM_JOB_SUBSCRIPTION_ID", "BOM_JOB_RESOURCE_GROUP", "PACKAGE_EVIDENCE_JOB_NAME", "PACKAGE_EVIDENCE_JOB_IMAGE")
            if any(not os.environ.get(name, "").strip() for name in required):
                raise RuntimeError("evidence job settings unavailable")
            credential = ManagedIdentityCredential(client_id=os.environ.get("BOM_JOB_MANAGED_IDENTITY_CLIENT_ID") or None)
            starter = AzureCandidateJobStarter(subscription_id=os.environ["BOM_JOB_SUBSCRIPTION_ID"],
                resource_group=os.environ["BOM_JOB_RESOURCE_GROUP"], job_name=os.environ["PACKAGE_EVIDENCE_JOB_NAME"],
                credential=credential, container_name=os.environ.get("PACKAGE_EVIDENCE_JOB_CONTAINER_NAME", "evidence-worker"),
                image=os.environ["PACKAGE_EVIDENCE_JOB_IMAGE"], resources={"cpu": 2, "memory": "4Gi"},
                base_environment={key: os.environ.get(key, "") for key in (
                    "AZURE_CLIENT_ID", "BOM_STORAGE_ACCOUNT_URL", "PACKAGE_EVIDENCE_MODEL_ENABLED",
                    "PACKAGE_EVIDENCE_MODEL_DEPLOYMENT", "FOUNDRY_PROJECT_ENDPOINT")})
            return EvidenceJobService(_storage_factory(), starter)
        evidence_api = EvidenceApi(authorize=lambda headers: authorize(headers, config),
            service_factory=evidence_service, policy=configured_policy(), enabled=True)
    server = ThreadingHTTPServer(
        (config.host, config.port), make_handler(api, decision_api, status_api, review_output_api, package_api, evidence_api, recovery_api))
    log.info("Deterministic BOM API listening on port %d", config.port)
    server.serve_forever()


if __name__ == "__main__":
    main()
