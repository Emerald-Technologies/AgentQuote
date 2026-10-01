"""Deterministic, prompt-independent BOM processing.

Validates processing requests, retrieves an explicitly selected parser and exact
staged sources, validates parser output, and publishes immutable outputs plus a
provenance manifest. Parser subprocesses still require an infrastructure sandbox
before this module can serve as a production security boundary.
"""
from __future__ import annotations

import hashlib
import itertools
import json
import math
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

import pandas as pd

from .blob_registry import BlobRegistryReader, RegistrySelection
from .master_registry import RegistryError
from .candidates import CandidateRequiredError, persist_candidate

CONTRACT_VERSION = "1"
OUTPUT_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
CANONICAL_COLUMNS = [
    "line_number", "item_number", "item_name", "revision", "item_category",
    "unit_of_measure", "quantity", "reference_designator", "bom_notes",
] + [column for index in range(1, 9) for column in (
    f"manufacturer_{index}", f"manufacturer_item_number_{index}"
)]
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$")
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
_SOURCE_EXTENSIONS = {".xls", ".xlsx", ".csv"}
_SAFE_FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,254}$")


class ProcessingError(ValueError):
    """A safe processing failure suitable for an API caller."""


class BlobConflictError(ProcessingError):
    """An immutable path already contains different bytes."""


class BlobNotFoundError(ProcessingError):
    """A requested blob does not exist."""


class Storage(Protocol):
    def download_source(self, path: str, max_bytes: int) -> bytes: ...
    def download_parser(self, parser_key: str, max_bytes: int) -> bytes: ...
    def download_output(self, path: str, max_bytes: int) -> bytes | None: ...
    def upload_output_immutable(self, path: str, data: bytes, content_type: str) -> None: ...
    def download_registry(self, path: str, max_bytes: int) -> bytes: ...
    def upload_registry_immutable(self, path: str, data: bytes, content_type: str) -> None: ...
    def download_candidate(self, path: str, max_bytes: int) -> bytes: ...
    def upload_candidate_immutable(self, path: str, data: bytes, content_type: str) -> None: ...
    def list_candidate_paths(self, prefix: str, max_results: int) -> list[str]: ...
    def list_registry_paths(self, prefix: str, max_results: int) -> list[str]: ...
    def download_registry_with_etag(self, path: str, max_bytes: int) -> tuple[bytes, str]: ...
    def upload_registry_if_match(self, path: str, data: bytes, content_type: str, etag: str | None) -> str: ...


class InMemoryStorage:
    """Storage implementation for tests and local contract validation."""

    def __init__(self, *, sources=None, parsers=None, outputs=None, candidates=None) -> None:
        self.sources = dict(sources or {})
        self.parsers = dict(parsers or {})
        self.outputs = dict(outputs or {})
        self.output_content_types: dict[str, str] = {}
        self.registry = {}
        self.registry_content_types: dict[str, str] = {}
        self.candidates = dict(candidates or {})
        self.candidate_content_types: dict[str, str] = {}

    def _registry_etag(self, path: str) -> str:
        return hashlib.sha256(self.registry[path]).hexdigest()

    @staticmethod
    def _bounded(data: bytes, max_bytes: int, description: str) -> bytes:
        if len(data) > max_bytes:
            raise ProcessingError(f"{description} exceeds limit")
        return bytes(data)

    def download_source(self, path: str, max_bytes: int) -> bytes:
        try:
            return self._bounded(self.sources[path], max_bytes, "source")
        except KeyError:
            raise BlobNotFoundError("source blob unavailable") from None

    def download_parser(self, parser_key: str, max_bytes: int) -> bytes:
        try:
            return self._bounded(self.parsers[f"{parser_key}/parse.py"], max_bytes, "parser")
        except KeyError:
            raise BlobNotFoundError("parser unavailable") from None

    def download_registry(self, path: str, max_bytes: int) -> bytes:
        try:
            return self._bounded(self.registry[path], max_bytes, "registry blob")
        except KeyError:
            raise BlobNotFoundError("registry blob unavailable") from None

    def download_output(self, path: str, max_bytes: int) -> bytes | None:
        data = self.outputs.get(path)
        return None if data is None else self._bounded(data, max_bytes, "stored output")

    def upload_registry_immutable(self, path: str, data: bytes, content_type: str) -> None:
        existing = self.registry.get(path)
        if existing is not None and existing != data:
            raise BlobConflictError("immutable registry conflict")
        if existing is None:
            self.registry[path] = bytes(data)
            self.registry_content_types[path] = content_type

    def download_registry_with_etag(self, path: str, max_bytes: int) -> tuple[bytes, str]:
        data = self.download_registry(path, max_bytes)
        return data, self._registry_etag(path)

    def upload_registry_if_match(self, path: str, data: bytes, content_type: str, etag: str | None) -> str:
        current = self.registry.get(path)
        if etag is None:
            if current is not None:
                raise BlobConflictError("registry create conflict")
        elif current is None or self._registry_etag(path) != etag:
            raise BlobConflictError("registry etag conflict")
        self.registry[path] = bytes(data)
        self.registry_content_types[path] = content_type
        return self._registry_etag(path)

    def upload_output_immutable(self, path: str, data: bytes, content_type: str) -> None:
        existing = self.outputs.get(path)
        if existing is not None and existing != data:
            raise BlobConflictError("immutable output conflict")
        if existing is None:
            self.outputs[path] = bytes(data)
            self.output_content_types[path] = content_type

    def download_candidate(self, path: str, max_bytes: int) -> bytes:
        try:
            return self._bounded(self.candidates[path], max_bytes, "candidate blob")
        except KeyError:
            raise BlobNotFoundError("candidate blob unavailable") from None

    def upload_candidate_immutable(self, path: str, data: bytes, content_type: str) -> None:
        existing = self.candidates.get(path)
        if existing is not None and existing != data:
            raise BlobConflictError("immutable candidate conflict")
        if existing is None:
            self.candidates[path] = bytes(data)
            self.candidate_content_types[path] = content_type

    def list_candidate_paths(self, prefix: str, max_results: int) -> list[str]:
        if not isinstance(prefix, str) or "\\" in prefix or not prefix.startswith("candidates/"):
            raise ProcessingError("invalid candidate prefix")
        if type(max_results) is not int or not 1 <= max_results <= 100:
            raise ProcessingError("invalid candidate list limit")
        paths = sorted({path for path in (*self.candidates.keys(), *self.registry.keys())
                        if path.startswith(prefix)})
        if len(paths) > max_results:
            raise ProcessingError("candidate list exceeds limit")
        return paths

    def list_registry_paths(self, prefix: str, max_results: int) -> list[str]:
        if not isinstance(prefix, str) or "\\" in prefix or not prefix.endswith("/"):
            raise ProcessingError("invalid registry prefix")
        if type(max_results) is not int or not 1 <= max_results <= 100:
            raise ProcessingError("invalid registry list limit")
        paths = sorted(path for path in self.registry.keys() if path.startswith(prefix))
        if len(paths) > max_results:
            raise ProcessingError("registry list exceeds limit")
        return paths


class AzureBlobStorage:
    """Azure Blob implementation using source and parser containers."""

    def __init__(self, connection_string: str, *, source_container="boms", parser_container="parsers") -> None:
        from azure.storage.blob import BlobServiceClient
        service = BlobServiceClient.from_connection_string(connection_string)
        self._sources = service.get_container_client(source_container)
        self._parsers = service.get_container_client(parser_container)

    @classmethod
    def from_account_url(cls, account_url: str, credential: Any, *,
                         source_container="boms", parser_container="parsers") -> "AzureBlobStorage":
        """Create storage using managed identity instead of an account secret."""
        from azure.storage.blob import BlobServiceClient
        instance = cls.__new__(cls)
        service = BlobServiceClient(account_url=account_url, credential=credential)
        instance._sources = service.get_container_client(source_container)
        instance._parsers = service.get_container_client(parser_container)
        return instance

    @staticmethod
    def _download_bounded(blob_client: Any, max_bytes: int) -> bytes:
        from azure.core.exceptions import ResourceNotFoundError
        try:
            properties = blob_client.get_blob_properties()
            if properties.size > max_bytes:
                raise ProcessingError("blob exceeds limit")
            data = blob_client.download_blob(max_concurrency=1).readall()
        except ProcessingError:
            raise
        except ResourceNotFoundError as exc:
            raise BlobNotFoundError("blob unavailable") from exc
        except Exception as exc:
            raise ProcessingError("blob download failed") from exc
        if len(data) > max_bytes:
            raise ProcessingError("blob exceeds limit")
        return data

    def download_source(self, path: str, max_bytes: int) -> bytes:
        return self._download_bounded(self._sources.get_blob_client(path), max_bytes)

    def download_parser(self, parser_key: str, max_bytes: int) -> bytes:
        return self._download_bounded(self._parsers.get_blob_client(f"{parser_key}/parse.py"), max_bytes)

    def download_registry(self, path: str, max_bytes: int) -> bytes:
        return self._download_bounded(self._parsers.get_blob_client(path), max_bytes)

    @staticmethod
    def _upload_immutable(container: Any, path: str, data: bytes, content_type: str,
                          artifact: str) -> None:
        """Create or verify an immutable blob without leaking SDK failures."""
        from azure.core.exceptions import ResourceExistsError
        from azure.storage.blob import ContentSettings
        client = container.get_blob_client(path)
        try:
            client.upload_blob(data, overwrite=False,
                               content_settings=ContentSettings(content_type=content_type))
            return
        except ResourceExistsError as exc:
            conflict_cause = exc
        except Exception as exc:
            raise ProcessingError(f"{artifact} upload failed") from exc
        try:
            properties = client.get_blob_properties()
            if properties.size != len(data):
                raise BlobConflictError(f"immutable {artifact} conflict") from conflict_cause
            existing = client.download_blob(
                offset=0, length=len(data) + 1, max_concurrency=1).readall()
            if len(existing) > len(data):
                raise BlobConflictError(f"immutable {artifact} conflict") from conflict_cause
        except BlobConflictError:
            raise
        except Exception as exc:
            raise ProcessingError(f"existing {artifact} verification failed") from exc
        if existing != data:
            raise BlobConflictError(f"immutable {artifact} conflict") from conflict_cause

    def upload_registry_immutable(self, path: str, data: bytes, content_type: str) -> None:
        self._upload_immutable(self._parsers, path, data, content_type, "registry")

    def download_registry_with_etag(self, path: str, max_bytes: int) -> tuple[bytes, str]:
        client = self._parsers.get_blob_client(path)
        data = self._download_bounded(client, max_bytes)
        try:
            return data, client.get_blob_properties().etag
        except Exception as exc:
            raise ProcessingError("registry properties failed") from exc

    def upload_registry_if_match(self, path: str, data: bytes, content_type: str, etag: str | None) -> str:
        client = self._parsers.get_blob_client(path)
        if etag is None:
            # Keep create-only pointer publication on the same SDK path as the
            # proven immutable artifact writes.  In particular, do not rely on
            # a second interpretation of upload_blob(overwrite=False) here:
            # immutable upload also handles an idempotent replay by comparing
            # the existing bytes after a create race.
            try:
                self.upload_registry_immutable(path, data, content_type)
            except ProcessingError as exc:
                if getattr(exc.__cause__, "status_code", None) == 409:
                    raise BlobConflictError("registry create conflict") from exc
                raise
            try:
                return client.get_blob_properties().etag
            except Exception as exc:
                raise ProcessingError("registry properties failed") from exc
        from azure.core import MatchConditions
        from azure.core.exceptions import HttpResponseError, ResourceExistsError, ResourceModifiedError, ResourceNotFoundError
        from azure.storage.blob import ContentSettings
        try:
            kwargs = {"content_settings": ContentSettings(content_type=content_type)}
            kwargs.update(overwrite=True, etag=etag, match_condition=MatchConditions.IfNotModified)
            client.upload_blob(data, **kwargs)
            return client.get_blob_properties().etag
        except (ResourceExistsError, ResourceModifiedError, ResourceNotFoundError) as exc:
            raise BlobConflictError("registry etag conflict") from exc
        except HttpResponseError as exc:
            if getattr(exc, "status_code", None) in {409, 412}:
                raise BlobConflictError("registry etag conflict") from exc
            raise ProcessingError("registry conditional upload failed") from exc
        except Exception as exc:
            raise ProcessingError("registry conditional upload failed") from exc

    def download_output(self, path: str, max_bytes: int) -> bytes | None:
        from azure.core.exceptions import ResourceNotFoundError
        try:
            return self._download_bounded(self._sources.get_blob_client(path), max_bytes)
        except BlobNotFoundError as exc:
            if isinstance(exc.__cause__, ResourceNotFoundError):
                return None
            raise

    def upload_output_immutable(self, path: str, data: bytes, content_type: str) -> None:
        self._upload_immutable(self._sources, path, data, content_type, "output")

    def download_candidate(self, path: str, max_bytes: int) -> bytes:
        return self._download_bounded(self._parsers.get_blob_client(path), max_bytes)

    def upload_candidate_immutable(self, path: str, data: bytes, content_type: str) -> None:
        self._upload_immutable(self._parsers, path, data, content_type, "candidate")

    def list_candidate_paths(self, prefix: str, max_results: int) -> list[str]:
        if (not isinstance(prefix, str) or "\\" in prefix or not prefix.startswith("candidates/")
                or ".." in prefix.split("/") or not prefix.endswith("/")):
            raise ProcessingError("invalid candidate prefix")
        if type(max_results) is not int or not 1 <= max_results <= 100:
            raise ProcessingError("invalid candidate list limit")
        try:
            blobs = self._parsers.list_blobs(name_starts_with=prefix)
            paths = [item.name for item in itertools.islice(blobs, max_results + 1)]
        except Exception as exc:
            raise ProcessingError("candidate list failed") from exc
        if len(paths) > max_results:
            raise ProcessingError("candidate list exceeds limit")
        return sorted(paths)

    def list_registry_paths(self, prefix: str, max_results: int) -> list[str]:
        if (not isinstance(prefix, str) or "\\" in prefix or ".." in prefix.split("/")
                or not prefix.endswith("/")):
            raise ProcessingError("invalid registry prefix")
        if type(max_results) is not int or not 1 <= max_results <= 100:
            raise ProcessingError("invalid registry list limit")
        try:
            blobs = self._parsers.list_blobs(name_starts_with=prefix)
            paths = [item.name for item in itertools.islice(blobs, max_results + 1)]
        except Exception as exc:
            raise ProcessingError("registry list failed") from exc
        if len(paths) > max_results:
            raise ProcessingError("registry list exceeds limit")
        return sorted(paths)


@dataclass(frozen=True)
class SourceDocument:
    document_id: str
    blob_path: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class ProcessingRequest:
    job_id: str
    customer_key: str
    parser_key: str
    rfq_uuid: str
    package_version: str
    documents: tuple[SourceDocument, ...]
    force_candidate: bool = False

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ProcessingRequest":
        if not isinstance(value, dict) or not isinstance(value.get("documents"), list):
            raise ProcessingError("invalid request")
        try:
            unknown = set(value) - {"job_id", "customer_key", "parser_key", "rfq_uuid",
                                    "package_version", "documents", "force_candidate"}
            if unknown or value.get("force_candidate", False) not in {False, True}:
                raise ProcessingError("invalid request")
            request = cls(
                value.get("job_id", ""), value.get("customer_key", ""),
                value.get("parser_key", ""), value.get("rfq_uuid", ""),
                value.get("package_version", ""),
                tuple(SourceDocument(**document) for document in value["documents"]),
                value.get("force_candidate", False),
            )
        except (TypeError, KeyError) as exc:
            raise ProcessingError("invalid request") from exc
        request.validate()
        return request

    def validate(self) -> None:
        for value in (self.job_id, self.customer_key, self.parser_key, self.package_version):
            if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
                raise ProcessingError("invalid identifier")
        if not isinstance(self.rfq_uuid, str) or not _UUID.fullmatch(self.rfq_uuid):
            raise ProcessingError("invalid rfq identifier")
        if not 1 <= len(self.documents) <= 100:
            raise ProcessingError("invalid document count")
        expected_prefix = f"{self.customer_key}/rfq-{self.rfq_uuid}/package-{self.package_version}/original-boms/"
        document_ids: set[str] = set()
        paths: set[str] = set()
        for document in self.documents:
            if (not isinstance(document.document_id, str)
                    or not _IDENTIFIER.fullmatch(document.document_id)
                    or document.document_id in document_ids):
                raise ProcessingError("invalid document id")
            document_ids.add(document.document_id)
            path = document.blob_path
            if (not isinstance(path, str) or path in paths or not path.startswith(expected_prefix)
                    or "\\" in path or any(ord(character) < 32 for character in path)):
                raise ProcessingError("invalid source path")
            paths.add(path)
            relative_name = path[len(expected_prefix):]
            pure_path = PurePosixPath(relative_name)
            if (not relative_name or len(pure_path.parts) != 1 or pure_path.name != relative_name
                    or not _SAFE_FILENAME.fullmatch(relative_name)
                    or pure_path.suffix.lower() not in _SOURCE_EXTENSIONS):
                raise ProcessingError("invalid source path")
            if (type(document.size_bytes) is not int or document.size_bytes < 0
                    or not isinstance(document.sha256, str)
                    or not _SHA256.fullmatch(document.sha256)):
                raise ProcessingError("invalid source metadata")

    def fingerprint(self) -> str:
        payload = {
            "version": CONTRACT_VERSION, "job_id": self.job_id,
            "customer_key": self.customer_key, "parser_key": self.parser_key,
            "rfq_uuid": self.rfq_uuid.lower(), "package_version": self.package_version,
            **({"force_candidate": True} if self.force_candidate else {}),
            "documents": [
                {"document_id": document.document_id, "blob_path": document.blob_path,
                 "size_bytes": document.size_bytes, "sha256": document.sha256.lower()}
                for document in self.documents
            ],
        }
        return _sha256(_json_bytes(payload))


class BomProcessor:
    """Execute trusted parsers and publish verified, immutable artifacts."""

    def __init__(self, storage: Storage, *, timeout_seconds=60,
                 max_source_bytes=50_000_000, max_total_source_bytes=100_000_000,
                 max_parser_bytes=1_000_000, max_output_bytes=50_000_000,
                 max_rows=100_000, registry_reader: BlobRegistryReader | None = None,
                 candidate_clock=None) -> None:
        limits = (timeout_seconds, max_source_bytes, max_total_source_bytes,
                  max_parser_bytes, max_output_bytes, max_rows)
        if any(type(limit) is not int or limit <= 0 for limit in limits):
            raise ValueError("processing limits must be positive integers")
        self.storage = storage
        self.timeout_seconds = timeout_seconds
        self.max_source_bytes = max_source_bytes
        self.max_total_source_bytes = max_total_source_bytes
        self.max_parser_bytes = max_parser_bytes
        self.max_output_bytes = max_output_bytes
        self.max_rows = max_rows
        self.registry_reader = registry_reader or BlobRegistryReader(storage.download_registry, max_bytes=max_parser_bytes)
        self.candidate_clock = candidate_clock

    def process(self, request: ProcessingRequest) -> dict[str, Any]:
        request.validate()
        if any(document.size_bytes > self.max_source_bytes for document in request.documents):
            raise ProcessingError("source exceeds limit")
        if sum(document.size_bytes for document in request.documents) > self.max_total_source_bytes:
            raise ProcessingError("request exceeds aggregate source limit")
        prefix = f"{request.customer_key}/rfq-{request.rfq_uuid}/package-{request.package_version}/processed/{request.job_id}"
        manifest_path = f"{prefix}/manifest.json"
        request_fingerprint = request.fingerprint()
        if request.force_candidate:
            first_source = self.storage.download_source(request.documents[0].blob_path, self.max_source_bytes)
            self._require_candidate(request, first_source, ProcessingError("replacement requested"))
        existing_manifest = self.storage.download_output(manifest_path, self.max_output_bytes)
        if existing_manifest is not None:
            return self._validate_existing_manifest(existing_manifest, request, request_fingerprint)

        registry_selection = None
        first_source = self.storage.download_source(request.documents[0].blob_path, self.max_source_bytes)
        try:
            registry_selection = self.registry_reader.select(request.parser_key, request.customer_key,
                                                              first_source,
                                                              PurePosixPath(request.documents[0].blob_path).suffix)
            parser_bytes = registry_selection.code
        except RegistryError as exc:
            if exc.code == "REGISTRY_ABSENT":
                try:
                    parser_bytes = self.storage.download_parser(request.parser_key, self.max_parser_bytes)
                except BlobNotFoundError:
                    self._require_candidate(request, first_source, exc)
            elif exc.code == "PARSER_FORMAT_NOT_RECOGNIZED":
                self._require_candidate(request, first_source, exc)
            else:
                raise ProcessingError(f"registry:{exc.code}") from exc
        parser_hash = _sha256(parser_bytes)
        pending_outputs: list[tuple[str, bytes]] = []
        manifest_outputs: list[dict[str, Any]] = []
        with tempfile.TemporaryDirectory(prefix="agentquote-") as temp_name:
            temp_root = Path(temp_name)
            parser_path = temp_root / "parse.py"
            parser_path.write_bytes(parser_bytes)
            for index, document in enumerate(request.documents):
                source = self.storage.download_source(document.blob_path, self.max_source_bytes)
                if len(source) != document.size_bytes or _sha256(source).lower() != document.sha256.lower():
                    raise ProcessingError("source integrity check failed")
                document_root = temp_root / str(index)
                document_root.mkdir()
                input_path = document_root / f"input{PurePosixPath(document.blob_path).suffix}"
                output_path = document_root / "output.xlsx"
                input_path.write_bytes(source)
                self._run_parser(parser_path, input_path, output_path, document_root)
                if not output_path.is_file() or not 0 < output_path.stat().st_size <= self.max_output_bytes:
                    raise ProcessingError("parser output invalid")
                _validate_xlsx(output_path, self.max_rows)
                output = output_path.read_bytes()
                output_blob_path = f"{prefix}/{document.document_id}-parsed.xlsx"
                pending_outputs.append((output_blob_path, output))
                manifest_outputs.append({
                    "source": {"document_id": document.document_id, "blob_path": document.blob_path,
                               "size_bytes": document.size_bytes, "sha256": document.sha256.lower()},
                    "output": {"blob_path": output_blob_path, "size_bytes": len(output),
                               "sha256": _sha256(output), "content_type": OUTPUT_CONTENT_TYPE},
                })
        manifest = {
            "version": CONTRACT_VERSION, "status": "completed", "job_id": request.job_id,
            "request_fingerprint": request_fingerprint, "customer_key": request.customer_key,
            "rfq_uuid": request.rfq_uuid.lower(), "package_version": request.package_version,
            "parser": {"key": request.parser_key, "blob_path": f"{request.parser_key}/parse.py",
                       "sha256": parser_hash},
            "results": manifest_outputs,
        }
        if registry_selection is not None:
            manifest["parser"].update({"variant_id": registry_selection.variant.variant_id,
                "version": registry_selection.variant.version, "signature_hash": registry_selection.variant.signature_hash,
                "rule_hash": registry_selection.variant.rule_hash, "approval_id": registry_selection.variant.approval_id,
                "selection_mode": "registry", "registry_generation": registry_selection.generation,
                "blob_path": registry_selection.variant.code_path})
        else:
            manifest["parser"]["selection_mode"] = "legacy"
        for output_path, output in pending_outputs:
            self.storage.upload_output_immutable(output_path, output, OUTPUT_CONTENT_TYPE)
        self.storage.upload_output_immutable(manifest_path, _json_bytes(manifest), "application/json")
        return manifest

    def _require_candidate(self, request: ProcessingRequest, first_source: bytes,
                           cause: Exception) -> None:
        profile = __import__("bom_processor.master_registry", fromlist=["profile_bytes"]).profile_bytes(
            first_source, PurePosixPath(request.documents[0].blob_path).suffix)
        for document in request.documents[1:]:
            other = __import__("bom_processor.master_registry", fromlist=["profile_bytes"]).profile_bytes(
                self.storage.download_source(document.blob_path, self.max_source_bytes),
                PurePosixPath(document.blob_path).suffix)
            if other.profile_hash != profile.profile_hash:
                raise ProcessingError("parser variant mismatch") from cause
        evidence = ({"document_id": request.documents[0].document_id,
                     "blob_path": request.documents[0].blob_path,
                     "size_bytes": request.documents[0].size_bytes,
                     "sha256": request.documents[0].sha256.lower()},)
        candidate = persist_candidate(self.storage, request.parser_key, request.customer_key,
                                      profile, evidence, self.candidate_clock)
        raise CandidateRequiredError(candidate.request_id, request.parser_key,
                                     profile.profile_hash) from cause

    def _run_parser(self, parser_path: Path, input_path: Path, output_path: Path,
                    working_directory: Path) -> None:
        try:
            subprocess.run(
                [sys.executable, str(parser_path), "--input", str(input_path), "--output", str(output_path)],
                check=True, shell=False, timeout=self.timeout_seconds, cwd=working_directory,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired as exc:
            raise ProcessingError("parser timed out") from exc
        except (subprocess.CalledProcessError, OSError) as exc:
            raise ProcessingError("parser failed") from exc

    def _validate_existing_manifest(self, raw_manifest: bytes, request: ProcessingRequest,
                                    request_fingerprint: str) -> dict[str, Any]:
        try:
            manifest = json.loads(raw_manifest)
        except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BlobConflictError("existing manifest is invalid") from exc
        if (not isinstance(manifest, dict) or manifest.get("version") != CONTRACT_VERSION
                or manifest.get("status") != "completed" or manifest.get("job_id") != request.job_id
                or manifest.get("request_fingerprint") != request_fingerprint):
            raise BlobConflictError("job id already used by a different request")
        results = manifest.get("results")
        if not isinstance(results, list) or len(results) != len(request.documents):
            raise BlobConflictError("existing manifest is invalid")
        for result in results:
            output = result.get("output") if isinstance(result, dict) else None
            if not isinstance(output, dict):
                raise BlobConflictError("existing manifest is invalid")
            path = output.get("blob_path")
            expected_size = output.get("size_bytes")
            expected_hash = output.get("sha256")
            if (not isinstance(path, str) or type(expected_size) is not int
                    or expected_size <= 0 or not isinstance(expected_hash, str)
                    or not _SHA256.fullmatch(expected_hash)):
                raise BlobConflictError("existing manifest is invalid")
            stored = self.storage.download_output(path, min(expected_size, self.max_output_bytes) + 1)
            if stored is None or len(stored) != expected_size or _sha256(stored) != expected_hash.lower():
                raise BlobConflictError("completed job output is missing or changed")
        return manifest


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_bytes(value: dict[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _is_present(value: Any) -> bool:
    return not pd.isna(value) and not (isinstance(value, str) and value == "")


def _validate_xlsx(path: Path, max_rows: int) -> None:
    try:
        frame = pd.read_excel(path, engine="openpyxl")
    except Exception as exc:
        raise ProcessingError("parsed workbook invalid") from exc
    if list(frame.columns) != CANONICAL_COLUMNS or frame.empty or len(frame) > max_rows:
        raise ProcessingError("parsed schema invalid")
    line_numbers = pd.to_numeric(frame["line_number"], errors="coerce")
    if (line_numbers.isna().any() or not line_numbers.map(lambda value: float(value).is_integer()).all()
            or line_numbers.astype(int).tolist() != list(range(1, len(frame) + 1))):
        raise ProcessingError("invalid line numbers")
    string_columns = [column for column in CANONICAL_COLUMNS if column not in {"line_number", "quantity"}]
    required_strings = {"item_number", "item_name", "item_category"}
    for column in string_columns:
        for value in frame[column].tolist():
            if pd.isna(value):
                if column in required_strings:
                    raise ProcessingError("required value invalid")
                continue
            # Identifier-like columns may legitimately hold numeric cells
            # (e.g. an item number 500009 stored as an int by the source
            # workbook). Coerce numbers to their canonical string form;
            # bools are never valid cell values here.
            if isinstance(value, bool):
                raise ProcessingError("string value invalid")
            if isinstance(value, (int, float)):
                value = str(int(value)) if float(value).is_integer() else str(value)
            if not isinstance(value, str) or not value or value != value.strip():
                raise ProcessingError("string value invalid")
    if not frame["item_category"].isin(["Part", "Document"]).all():
        raise ProcessingError("item category invalid")
    quantities = pd.to_numeric(frame["quantity"], errors="coerce")
    if quantities.isna().any() or not quantities.map(math.isfinite).all():
        raise ProcessingError("quantity invalid")
    for index in range(1, 9):
        manufacturers = frame[f"manufacturer_{index}"].map(_is_present)
        part_numbers = frame[f"manufacturer_item_number_{index}"].map(_is_present)
        if not manufacturers.equals(part_numbers):
            raise ProcessingError("manufacturer pair invalid")
