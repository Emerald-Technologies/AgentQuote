"""Injected jobs/in-memory storage only; no Azure, model service or customer data."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from threading import Lock

import pytest

from bom_processor.core import InMemoryStorage, BlobConflictError
from bom_processor.package_evidence_contracts import PackageEvidenceError, json_bytes, object_hash, digest
from bom_processor.package_evidence_jobs import EvidenceJobService, evidence_request
from test_package_evidence_proposal import inputs, FakeModel


class AtomicStorage(InMemoryStorage):
    def __init__(self):
        super().__init__()
        self.lock = Lock()

    def upload_registry_immutable(self, path, data, content_type):
        with self.lock:
            return super().upload_registry_immutable(path, data, content_type)


class Starter:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def start(self, environment):
        self.calls.append(environment)
        if self.fail:
            raise TimeoutError("sensitive raw Azure details")
        return "execution-one"


def setup(fail=False):
    context, roots, books = inputs()
    storage = AtomicStorage()
    prefix = f'evidence-inputs/{context["qqUuid"]}/package-{context["packageVersion"]}/'
    references = []
    for root in roots:
        path = f'{prefix}{root["documentId"]}/{root["sha256"]}'
        storage.sources[path] = root["data"]
        references.append({k: root[k] for k in ("documentId", "sha256", "sizeBytes", "fileName")} | {"blobPath": path})
    workbook_refs = []
    for document, data in books.items():
        bom = next(a["canonicalBom"] for a in context["assemblies"] if a["canonicalBom"]["documentId"] == document)
        path = f'{prefix}{document}/{bom["sha256"]}'
        storage.sources[path] = data
        workbook_refs.append({**bom, "blobPath": path})
    request = evidence_request(context, references, workbook_refs,
        {"producer": "offline-test", "model": "fake-model", "policyVersion": "v1", "promptSha256": "d" * 64})
    starter = Starter(fail)
    return EvidenceJobService(storage, starter), request, storage, starter


def test_submit_status_side_effect_free_and_deterministic():
    service, request, storage, starter = setup()
    assert service.submit(request) == {"requestId": request["requestId"], "status": "requested"}
    assert service.submit(deepcopy(request))["status"] == "requested"
    for _ in range(3):
        assert service.status(request["requestId"])["status"] == "requested"
    assert len(storage.registry) == 1 and not starter.calls
    assert service.request(request["requestId"]) == request


def test_start_replay_and_concurrent_start_only_one_execution():
    service, request, _, starter = setup()
    service.submit(request)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: service.start(request["requestId"]), range(8)))
    assert len(starter.calls) == 1
    assert starter.calls[0] == {"REQUEST_ID": request["requestId"]}
    assert service.start(request["requestId"])["status"] == "started"


def test_ambiguous_start_never_retried_or_exposes_details():
    service, request, storage, starter = setup(True)
    service.submit(request)
    assert service.start(request["requestId"])["status"] == "start_unknown"
    assert service.start(request["requestId"])["status"] == "start_unknown"
    assert len(starter.calls) == 1
    assert b"sensitive" not in b"".join(storage.registry.values())


def test_run_real_offline_producer_artifact_completion_and_replay():
    service, request, storage, starter = setup()
    service.submit(request)
    model = FakeModel()
    result = service.run(request["requestId"], model)
    assert result["status"] == "completed"
    graph = result["proposal"]
    assert graph["provenance"]["runId"] == request["requestId"]
    assert graph["qqUuid"] == request["context"]["qqUuid"]
    assert "approvalSha256" not in graph
    second = FakeModel()
    assert service.run(request["requestId"], second) == result
    assert second.packet is None
    assert service.start(request["requestId"]) == result and not starter.calls
    assert b"UNTRUSTED_DATA" not in storage.registry[service._path(request["requestId"], "proposal")]


def test_worker_duplicate_claim_does_not_reinvoke_model():
    service, request, _, _ = setup()
    service.submit(request)
    service._write(request["requestId"], "worker-claim", {"requestId": request["requestId"], "claimId": "a" * 32})
    model = FakeModel()
    assert service.run(request["requestId"], model)["status"] == "running"
    assert model.packet is None


def test_tampered_source_fails_before_model_and_no_retry():
    service, request, storage, _ = setup()
    service.submit(request)
    storage.sources[request["roots"][0]["blobPath"]] = b"tampered"
    model = FakeModel()
    with pytest.raises(PackageEvidenceError, match="EVIDENCE_JOB_FAILED"):
        service.run(request["requestId"], model)
    assert model.packet is None
    assert service.status(request["requestId"])["status"] == "failed"
    assert service.run(request["requestId"], model)["status"] == "failed"


class ReplyModel(FakeModel):
    """FakeModel that records a raw reply like FoundryEvidenceProposalModel does."""

    def __init__(self, reply, change=None):
        super().__init__(change)
        self.reply = reply
        self.last_producer_reply = None

    def propose(self, packet):
        self.last_producer_reply = self.reply
        output = super().propose(packet)
        output["associations"].append(deepcopy(output["associations"][0]))  # DUPLICATE_ASSOCIATION
        return output


def _diagnostic(service, storage, request):
    import json
    raw = storage.registry.get(service._path(request["requestId"], "failure-diagnostic"))
    return None if raw is None else json.loads(raw)


def test_failed_producer_reply_is_kept_as_protected_diagnostic():
    service, request, storage, _ = setup()
    service.submit(request)
    reply = '{"associations": ["exact model text"]}'
    with pytest.raises(PackageEvidenceError, match="EVIDENCE_JOB_FAILED"):
        service.run(request["requestId"], ReplyModel(reply))
    record = _diagnostic(service, storage, request)
    assert record == {"version": "package-evidence-failure-diagnostic.v1", "requestId": request["requestId"],
                      "causes": ["DUPLICATE_ASSOCIATION"],
                      "producerReply": {"sha256": digest(reply.encode()), "sizeBytes": len(reply.encode()),
                                        "text": reply, "omitted": None}}
    # Diagnostic never changes status or blocks the no-retry rule.
    assert service.status(request["requestId"])["status"] == "failed"


def test_failure_without_reply_records_causes_only_and_success_writes_nothing():
    service, request, storage, _ = setup()
    service.submit(request)
    storage.sources[request["roots"][0]["blobPath"]] = b"tampered"
    with pytest.raises(PackageEvidenceError):
        service.run(request["requestId"], FakeModel())
    assert _diagnostic(service, storage, request) == {
        "version": "package-evidence-failure-diagnostic.v1", "requestId": request["requestId"],
        "causes": ["SOURCE_IDENTITY_MISMATCH"], "producerReply": None}
    ok_service, ok_request, ok_storage, _ = setup()
    ok_service.submit(ok_request)
    assert ok_service.run(ok_request["requestId"], FakeModel())["status"] == "completed"
    assert _diagnostic(ok_service, ok_storage, ok_request) is None


def test_oversize_reply_is_hashed_not_stored():
    from bom_processor.package_evidence_jobs import MAX_DIAGNOSTIC_REPLY_BYTES
    service, request, storage, _ = setup()
    service.submit(request)
    reply = "x" * (MAX_DIAGNOSTIC_REPLY_BYTES + 1)
    with pytest.raises(PackageEvidenceError):
        service.run(request["requestId"], ReplyModel(reply))
    record = _diagnostic(service, storage, request)["producerReply"]
    assert record == {"sha256": digest(reply.encode()), "sizeBytes": len(reply), "text": None,
                      "omitted": "REPLY_TOO_LARGE"}


def test_diagnostic_write_failure_never_changes_the_outcome():
    service, request, storage, _ = setup()
    service.submit(request)
    original = storage.upload_registry_immutable

    def refuse_diagnostic(path, data, content_type):
        if path.endswith("/failure-diagnostic.json"):
            raise RuntimeError("storage unavailable")
        return original(path, data, content_type)
    storage.upload_registry_immutable = refuse_diagnostic
    with pytest.raises(PackageEvidenceError, match="EVIDENCE_JOB_FAILED") as caught:
        service.run(request["requestId"], ReplyModel("{}"))
    assert isinstance(caught.value.__cause__, PackageEvidenceError)
    assert str(caught.value.__cause__) == "DUPLICATE_ASSOCIATION"
    assert service.status(request["requestId"])["status"] == "failed"


@pytest.mark.parametrize("change", [
    lambda r: r.update(requestId="0" * 64),
    lambda r: r.update(approved=True),
    lambda r: r["roots"][0].update(blobPath="https://arbitrary.example/file"),
    lambda r: r["context"].update(predecessorProposalSha256="f" * 64),
    lambda r: r["workbooks"].clear(),
    lambda r: r["roots"].append(deepcopy(r["roots"][0])),
    lambda r: r["roots"][0].update(sizeBytes=True),
    lambda r: r["policy"].update(model=""),
])
def test_invalid_request_no_artifacts_or_starts(change):
    service, request, storage, starter = setup()
    change(request)
    with pytest.raises(PackageEvidenceError):
        service.submit(request)
    assert not storage.registry and not starter.calls


def test_missing_request_and_storage_fault_fail_loud():
    service, request, _, _ = setup()
    with pytest.raises(PackageEvidenceError, match="REQUEST_NOT_FOUND"):
        service.status(request["requestId"])
    def unavailable(*args):
        raise RuntimeError("storage unavailable")
    service.storage.download_registry = unavailable
    with pytest.raises(RuntimeError, match="storage unavailable"):
        service.status(request["requestId"])


def test_corrupt_result_rejected():
    service, request, storage, _ = setup()
    service.submit(request)
    service.run(request["requestId"], FakeModel())
    path = service._path(request["requestId"], "proposal")
    graph = service.status(request["requestId"])["proposal"]
    graph["qqUuid"] = "12345678-1234-4234-8234-123456789999"
    storage.registry[path] = json_bytes(graph)
    with pytest.raises(PackageEvidenceError, match="RESULT_CORRUPT"):
        service.status(request["requestId"])


def test_noncanonical_duplicate_json_artifact_rejected():
    service, request, storage, _ = setup()
    service.submit(request)
    storage.registry[service._path(request["requestId"], "request")] = b'{"x":1,"x":2}'
    with pytest.raises(PackageEvidenceError, match="ARTIFACT_CORRUPT"):
        service.status(request["requestId"])


def test_stored_request_immutable_conflict():
    service, request, storage, _ = setup()
    service.submit(request)
    storage.registry[service._path(request["requestId"], "request")] = b"different"
    with pytest.raises(BlobConflictError):
        service.submit(request)


@pytest.mark.parametrize("artifact,value", [
    ("completed", {}), ("worker-claim", {"requestId": "placeholder"}),
    ("failed", {"requestId": "placeholder"}), ("started", {"requestId": "placeholder"}),
    ("start-unknown", {"requestId": "placeholder", "code": "wrong"}),
])
def test_corrupt_markers_fail_closed(artifact, value):
    service, request, _, _ = setup()
    service.submit(request)
    if "requestId" in value:
        value["requestId"] = request["requestId"]
    service._write(request["requestId"], artifact, value)
    with pytest.raises(PackageEvidenceError):
        service.status(request["requestId"])


@pytest.mark.parametrize("after", [False, True])
def test_completion_publication_recovery_without_reexecution(after):
    service, request, storage, _ = setup()
    service.submit(request)
    original = storage.upload_registry_immutable
    def failing(path, data, content_type):
        if path.endswith("/completed.json"):
            if after:
                original(path, data, content_type)
            raise RuntimeError("publication uncertain")
        original(path, data, content_type)
    storage.upload_registry_immutable = failing
    with pytest.raises(PackageEvidenceError, match="PUBLICATION_UNKNOWN"):
        service.run(request["requestId"], FakeModel())
    storage.upload_registry_immutable = original
    assert service.reconcile_publication(request["requestId"])["status"] == "completed"
    second = FakeModel()
    assert service.run(request["requestId"], second)["status"] == "completed"
    assert second.packet is None


def test_recomputed_foreign_result_rejected():
    service, request, storage, _ = setup()
    service.submit(request)
    graph = service.run(request["requestId"], FakeModel())["proposal"]
    graph["packageVersion"] = 2
    graph["proposalSha256"] = object_hash({k: v for k, v in graph.items() if k != "proposalSha256"})
    storage.registry[service._path(request["requestId"], "proposal")] = json_bytes(graph)
    storage.registry[service._path(request["requestId"], "completed")] = json_bytes({"requestId": request["requestId"],
        "proposalSha256": graph["proposalSha256"], "artifactSha256": digest(json_bytes(graph))})
    with pytest.raises(PackageEvidenceError, match="RESULT_BINDING"):
        service.status(request["requestId"])
    with pytest.raises(PackageEvidenceError, match="RESULT_BINDING"):
        service.reconcile_publication(request["requestId"])