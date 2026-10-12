import hashlib
import io
import json
import sys
import types
from datetime import datetime, timezone

import pandas as pd
import pytest

from bom_processor import BlobRegistryReader, InMemoryStorage, RegistryError, StartResult, VariantSignature
from bom_processor.admin_promotion import AdminDecisionService
from bom_processor.candidate_authoring import CandidateAuthoringService
from bom_processor.candidate_validation import CandidateValidationService
from bom_processor.candidates import persist_candidate
from bom_processor.core import BlobConflictError, CANONICAL_COLUMNS, OUTPUT_CONTENT_TYPE
from bom_processor.core import AzureBlobStorage, ProcessingError
from bom_processor.master_registry import profile_bytes
from row_map_fixtures import attach_row_map

SOURCE_PATH = "customer/rfq-id/package-1/original-boms/doc.csv"
# Holds every value workbook() writes, so row accounting (plan 8.8 step 4a) can cite it.
# A maker pair: a BOM without any manufacturer/MPN pair stops for a person (round 8).
SOURCE = b"mystery,name,qty,maker,maker part\nX,Widget,2,Acme,A-100\n"
CODE = b'''import argparse\nparser = argparse.ArgumentParser()\nparser.add_argument("--input", required=True)\nparser.add_argument("--output", required=True)\nparser.add_argument("--row-map", required=True)\nargs = parser.parse_args()\n'''
NOW = datetime(2026, 9, 21, 3, 0, tzinfo=timezone.utc)


def workbook():
    row = {column: None for column in CANONICAL_COLUMNS}
    row.update(line_number=1, item_number="X", item_name="Widget",
               item_category="Part", quantity=2.0, manufacturer_1="Acme", manufacturer_item_number_1="A-100")
    stream = io.BytesIO()
    pd.DataFrame([row], columns=CANONICAL_COLUMNS).to_excel(stream, index=False)
    return stream.getvalue()


class Model:
    def generate(self, prompt, source_bytes):
        return CODE.decode()


class Worker:
    def __init__(self, storage):
        self.storage = storage
        self.calls = []

    def start(self, item):
        self.calls.append(item)
        output = workbook()
        self.storage.candidates[item.output_blob] = output
        result = {
            "contract_version": "remote-worker.v1", "job": item.job_id, "status": "completed",
            "input": {"verified": True, "sha256": item.input_sha256, "size": item.input_size},
            "parser": {"verified": True, "sha256": item.parser_sha256, "size": item.parser_size},
            "output": {"sha256": hashlib.sha256(output).hexdigest(), "size": len(output),
                       "content_type": OUTPUT_CONTENT_TYPE},
        }
        result = attach_row_map(self.storage, item, output, result)
        self.storage.candidates[item.result_blob] = json.dumps(result).encode()
        return StartResult(item.job_id, "worker-execution", NOW)


def prepared():
    storage = InMemoryStorage(sources={SOURCE_PATH: SOURCE})
    profile = profile_bytes(SOURCE, ".csv")
    request = persist_candidate(storage, "Customer1", "customer-key", profile, ({
        "document_id": "doc1", "blob_path": SOURCE_PATH,
        "size_bytes": len(SOURCE), "sha256": hashlib.sha256(SOURCE).hexdigest(),
    },), clock=lambda: NOW)
    candidate = CandidateAuthoringService(request.request_id, storage, Model()).author()
    validation = CandidateValidationService(
        request.request_id, candidate.candidate_id, storage, Worker(storage),
        sleep=lambda _: None).validate()
    service = AdminDecisionService(storage, clock=lambda: NOW)
    common = dict(request_id=request.request_id, candidate_id=candidate.candidate_id,
                  validation_id=validation.validation_id, approver={"object_id": "admin-object-1"},
                  reason="Reviewed synthetic fixture and validation evidence", ticket="CHG-1001")
    return storage, service, common


def test_rejection_is_immutable_and_never_promotes():
    storage, service, common = prepared()
    result = service.decide(decision="reject", **common)
    assert result["decision"] == "reject"
    assert service.decide(decision="reject", **common) == result
    assert any(path.startswith("candidates/decisions/") for path in storage.registry)
    assert any("candidate.rejected" in data.decode() for data in storage.registry.values())
    assert not any(path.startswith("approved/") or path.endswith("/master.json")
                   for path in storage.registry)


def test_approval_publishes_exact_code_and_blob_reader_selects_it():
    storage, service, common = prepared()
    result = service.decide(decision="approve", variant_id="mystery-csv", version="1",
                            **{**common, "reason": "", "ticket": ""})
    assert result["decision"] == "approve"
    assert result["parser"]["approval_id"] == result["decision_id"]
    assert result["parser"]["registry_generation"] == 1
    base = "approved/Customer1/mystery-csv/1"
    candidate_code = storage.candidates[
        f"candidates/{common['request_id']}/{common['candidate_id']}/parse.py"]
    assert storage.registry[f"{base}/parse.py"] == candidate_code
    selected = BlobRegistryReader(storage.download_registry).select(
        "Customer1", "customer-key", SOURCE, ".csv")
    assert selected.code == candidate_code
    assert selected.variant.variant_id == "mystery-csv"
    assert any("/generations/" in path for path in storage.registry)
    assert any("/events/" in path and path.startswith("registry/Customer1/")
               for path in storage.registry)


def test_approved_signature_is_derived_from_candidate_profile_for_same_source():
    storage, service, common = prepared()
    service.decide(decision="approve", variant_id="mystery-csv", version="1", **common)

    request = json.loads(storage.registry[f"candidates/requests/{common['request_id']}.json"])
    signature = json.loads(storage.registry[
        "approved/Customer1/mystery-csv/1/signature.json"])
    # The validated parser read the code-found heading row, so the heading rule is approved.
    assert signature["signature_hash"] == VariantSignature.from_profile(
        request["profile"], heading=True).signature_hash
    assert signature["required_heading"] == {"Sheet1": ["mystery", "name", "qty", "maker", "maker_part"]}
    selected = BlobRegistryReader(storage.download_registry).select(
        "Customer1", "customer-key", SOURCE, ".csv")
    assert selected.variant.signature_hash == signature["signature_hash"]


def test_approval_replay_is_idempotent_and_resumes_promotion():
    storage, service, common = prepared()
    first = service.decide(decision="approve", variant_id="mystery-csv", version="1", **common)
    before = dict(storage.registry)
    assert service.decide(decision="approve", variant_id="mystery-csv", version="1", **common) == first
    assert storage.registry == before


def test_conflicting_decision_or_variant_fails_closed():
    _, service, common = prepared()
    service.decide(decision="reject", **common)
    with pytest.raises(RegistryError, match="DECISION_CONFLICT"):
        service.decide(decision="approve", variant_id="mystery-csv", version="1", **common)


def test_invalid_approver_and_unbounded_audit_fields_are_rejected():
    _, service, common = prepared()
    common["approver"] = {"object_id": ""}
    with pytest.raises(RegistryError, match="INVALID_APPROVER"):
        service.decide(decision="reject", **common)
    common["approver"] = {"object_id": "admin-object-1"}
    common["reason"] = "x" * 2001
    with pytest.raises(RegistryError, match="INVALID_DECISION"):
        service.decide(decision="reject", **common)


def test_tampered_candidate_or_validation_evidence_fails_before_decision():
    storage, service, common = prepared()
    code_path = f"candidates/{common['request_id']}/{common['candidate_id']}/parse.py"
    storage.candidates[code_path] += b"# changed"
    with pytest.raises(RegistryError, match="CANDIDATE_BINDING"):
        service.decide(decision="reject", **common)
    assert not any(path.startswith("candidates/decisions/") for path in storage.registry)


def test_cas_conflict_rereads_and_preserves_concurrent_variant():
    storage, service, common = prepared()
    original = storage.upload_registry_if_match
    conflicted = False

    def conflict_once(path, data, content_type, etag):
        nonlocal conflicted
        if not conflicted and path.endswith("/master.json"):
            conflicted = True
            concurrent = {
                "schema": "master-registry.v1", "parser_key": "Customer1", "generation": 1,
                "variants": [{"variant_id": "existing", "version": "1", "code_path": "approved/Customer1/existing/1/parse.py",
                              "code_sha256": "1" * 64, "signature_hash": "2" * 64,
                              "rule_hash": "3" * 64, "approval_id": "existing-approval",
                              "signature_path": "approved/Customer1/existing/1/signature.json",
                              "approval_path": "approved/Customer1/existing/1/approval.json"}],
            }
            storage.registry[path] = json.dumps(concurrent, sort_keys=True, separators=(",", ":")).encode()
            raise BlobConflictError("simulated concurrent write")
        return original(path, data, content_type, etag)

    storage.upload_registry_if_match = conflict_once
    service.decide(decision="approve", variant_id="mystery-csv", version="1", **common)
    master = json.loads(storage.registry["registry/Customer1/master.json"])
    assert master["generation"] == 2
    assert {item["variant_id"] for item in master["variants"]} == {"existing", "mystery-csv"}


def test_candidate_code_is_never_executed(tmp_path):
    storage, service, common = prepared()
    marker = tmp_path / "executed"
    code_path = f"candidates/{common['request_id']}/{common['candidate_id']}/parse.py"
    code = storage.candidates[code_path] + f"\nfrom pathlib import Path\nPath({str(marker)!r}).touch()\n".encode()
    storage.candidates[code_path] = code
    manifest_path = f"candidates/{common['request_id']}/{common['candidate_id']}/candidate-manifest.json"
    manifest = json.loads(storage.candidates[manifest_path])
    manifest["code_sha256"] = hashlib.sha256(code).hexdigest()
    storage.candidates[manifest_path] = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    with pytest.raises(RegistryError):
        service.decide(decision="reject", **common)
    assert not marker.exists()


def test_transient_pointer_failure_is_retried_not_abandoned():
    storage, service, common = prepared()
    original = storage.upload_registry_if_match
    failed = {"once": False}

    def transient_once(path, data, content_type, etag):
        if path.endswith("/master.json") and not failed["once"]:
            failed["once"] = True
            raise ProcessingError("registry conditional upload failed")
        return original(path, data, content_type, etag)

    storage.upload_registry_if_match = transient_once
    service.decide(decision="approve", variant_id="mystery-csv", version="1", **common)
    master = json.loads(storage.registry["registry/Customer1/master.json"])
    assert master["generation"] == 1
    assert [item["variant_id"] for item in master["variants"]] == ["mystery-csv"]
    selected = BlobRegistryReader(storage.download_registry).select(
        "Customer1", "customer-key", SOURCE, ".csv")
    assert selected.variant.variant_id == "mystery-csv"


def test_azure_registry_create_uses_supported_create_only_kwargs_and_maps_conflict(monkeypatch):
    class ResourceExistsError(Exception):
        pass

    class ResourceModifiedError(Exception):
        pass

    class ResourceNotFoundError(Exception):
        pass

    class HttpResponseError(Exception):
        def __init__(self, status_code):
            super().__init__(status_code)
            self.status_code = status_code

    class ContentSettings:
        def __init__(self, *, content_type):
            self.content_type = content_type

    class MatchConditions:
        IfNotModified = "if-not-modified"

    exceptions = types.ModuleType("azure.core.exceptions")
    exceptions.ResourceExistsError = ResourceExistsError
    exceptions.ResourceModifiedError = ResourceModifiedError
    exceptions.ResourceNotFoundError = ResourceNotFoundError
    exceptions.HttpResponseError = HttpResponseError
    core = types.ModuleType("azure.core")
    core.MatchConditions = MatchConditions
    blob = types.ModuleType("azure.storage.blob")
    blob.ContentSettings = ContentSettings
    monkeypatch.setitem(sys.modules, "azure.core", core)
    monkeypatch.setitem(sys.modules, "azure.core.exceptions", exceptions)
    monkeypatch.setitem(sys.modules, "azure.storage.blob", blob)

    class Client:
        def __init__(self):
            self.calls = []
            self.etag = '"new"'
            self.error = None

        def upload_blob(self, data, **kwargs):
            self.calls.append((data, kwargs))
            if self.error:
                raise self.error

        def get_blob_properties(self):
            return types.SimpleNamespace(etag=self.etag)

    class Container:
        def __init__(self, client):
            self.client = client

        def get_blob_client(self, path):
            assert path == "registry/Customer1/master.json"
            return self.client

    client = Client()
    storage = AzureBlobStorage.__new__(AzureBlobStorage)
    storage._parsers = Container(client)

    assert storage.upload_registry_if_match(
        "registry/Customer1/master.json", b"master", "application/json", None) == '"new"'
    data, kwargs = client.calls[-1]
    assert data == b"master"
    assert kwargs["overwrite"] is False
    assert "if_none_match" not in kwargs
    assert kwargs["content_settings"].content_type == "application/json"

    client.error = HttpResponseError(409)
    with pytest.raises(BlobConflictError):
        storage.upload_registry_if_match(
            "registry/Customer1/master.json", b"different", "application/json", None)

    client.error = None
    storage.upload_registry_if_match(
        "registry/Customer1/master.json", b"master", "application/json", '"old"')
    _, cas_kwargs = client.calls[-1]
    assert cas_kwargs["overwrite"] is True
    assert cas_kwargs["etag"] == '"old"'
    assert cas_kwargs["match_condition"] == MatchConditions.IfNotModified
