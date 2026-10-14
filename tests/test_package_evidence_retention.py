"""Real synthetic ZIP/source pin regressions; no external services or samples."""
from copy import deepcopy

import pytest

from bom_processor.package_evidence_contracts import PackageEvidenceError, evidence_id
from bom_processor.package_evidence_proposal import produce_proposal
from bom_processor.package_evidence_retention import retained_inventory, validate_retained_sources
from bom_processor.package_evidence_inventory import build_inventory
from bom_processor.package_evidence_jobs import evidence_request
from test_package_evidence_inventory import root, archive
from test_package_evidence_proposal import U
from test_package_manufacturing_assessment import assessed_inputs, AssessedModel
from test_package_evidence_jobs import setup


def source_proof(roots):
    source = next(r for r in roots if r["fileName"].endswith(".xlsx"))
    return {"requestFingerprint": "e" * 64, "stagingEventId": "12345678-1234-4234-8234-123456789080",
            "sources": [{k: source[k] for k in ("documentId", "sha256", "sizeBytes")}]}


def retained_inputs():
    context, roots, books = assessed_inputs()
    roots.append(root(b"exact original source workbook bytes", "original.xlsx", U))
    roots.append(root(archive([("nested.zip", archive([])), ("unknown.bin", b"\x00\xff")]),
                      "package.zip", "12345678-1234-4234-8234-123456789081"))
    return context, roots, books


class RetentionModel(AssessedModel):
    def propose(self, packet):
        out = super().propose(packet)
        retained = {r["evidenceId"] for r in packet["retainedProvenance"]}
        out["dispositions"] = [d for d in out["dispositions"] if d["evidenceId"] not in retained]
        return out


def produce(model=None):
    context, roots, books = retained_inputs()
    return produce_proposal(context, roots, books, model=model or RetentionModel(),
        producer="offline-test", model_name="fake", policy_version="package-evidence-foundry.v4",
        prompt_sha256="d" * 64, run_id="test", include_classification=True, include_manufacturing=True,
        retained_bom_sources=source_proof(roots))


def test_actual_nested_empty_zip_and_original_bom_retention_keeps_unknown_child():
    model = RetentionModel()
    result = produce(model)
    graph = result["proposal"]
    retained = {r["evidenceId"]: r["kind"] for r in model.packet["retainedProvenance"]}
    assert sorted(retained.values()) == ["archive_container", "archive_container", "original_bom_input"]
    assert all(a["evidenceId"] not in retained for a in graph["associations"])
    assert all(d["state"] == "unrelated" for d in graph["dispositions"] if d["evidenceId"] in retained)
    child = next(e for e in graph["evidenceObjects"] if e["archiveChain"] and e["archiveChain"][-1]["memberPath"] == "unknown.bin")
    assert next(d for d in graph["dispositions"] if d["evidenceId"] == child["evidenceId"])["state"] == "unresolved"
    assert len(graph["evidenceObjects"]) == len(graph["dispositions"])
    assert len(result["manufacturingAssessment"]["relationships"]) == len(graph["associations"])


def test_model_cannot_grant_manufacturing_or_disposition_authority_to_retained_objects():
    class Invalid(RetentionModel):
        def propose(self, packet):
            out = super().propose(packet)
            out["associations"].append({"evidenceId": packet["retainedProvenance"][0]["evidenceId"]})
            return out
    with pytest.raises(PackageEvidenceError, match="RETAINED_MANUFACTURING_ASSOCIATION"):
        produce(Invalid())
    class Labeled(RetentionModel):
        def propose(self, packet):
            out = super().propose(packet)
            out["dispositions"].append({"evidenceId": packet["retainedProvenance"][0]["evidenceId"]})
            return out
    with pytest.raises(PackageEvidenceError, match="MODEL_RETENTION_AUTHORITY"):
        produce(Labeled())


def test_source_reference_tamper_drawing_and_unbound_spreadsheet_fail():
    _, roots, _ = retained_inputs()
    proof = source_proof(roots)
    assert validate_retained_sources(proof, roots) == {U}
    bad = deepcopy(proof)
    bad["sources"][0]["sha256"] = "f" * 64
    with pytest.raises(PackageEvidenceError, match="BINDING"):
        validate_retained_sources(bad, roots)
    bad["sources"] = [{k: roots[0][k] for k in ("documentId", "sha256", "sizeBytes")}]
    with pytest.raises(PackageEvidenceError, match="BINDING"):
        validate_retained_sources(bad, roots)
    extra = root(b"unbound spreadsheet", "other.xlsx", "12345678-1234-4234-8234-123456789082")
    roots.append(extra)
    retained = retained_inventory(build_inventory(roots), roots, proof)
    identity = {k: extra[k] for k in ("documentId", "sha256", "sizeBytes")} | {"archiveChain": []}
    assert evidence_id(identity) not in retained


def test_versioned_request_requires_retention_and_legacy_does_not_accept_it():
    context, roots, _ = retained_inputs()
    prefix = f'evidence-inputs/{context["qqUuid"]}/package-{context["packageVersion"]}/'
    refs = [{k: r[k] for k in ("documentId", "sha256", "sizeBytes", "fileName")} |
            {"blobPath": prefix + r["documentId"] + "/" + r["sha256"]} for r in roots]
    bookrefs = [a["canonicalBom"] | {"blobPath": prefix + a["canonicalBom"]["documentId"] + "/" + a["canonicalBom"]["sha256"]}
                for a in context["assemblies"]]
    policy = {"producer": "test", "model": "fake", "policyVersion": "package-evidence-foundry.v4", "promptSha256": "d" * 64}
    request = evidence_request(context, refs, bookrefs, policy, source_proof(roots))
    assert request["version"] == "package-evidence-request.v2"
    with pytest.raises(PackageEvidenceError, match="RETENTION_POLICY_BINDING"):
        evidence_request(context, refs, bookrefs, policy)
    policy["policyVersion"] = "package-evidence-foundry.v3"
    with pytest.raises(PackageEvidenceError, match="RETENTION_POLICY_BINDING"):
        evidence_request(context, refs, bookrefs, policy, source_proof(roots))


def test_v2_job_publishes_three_immutable_artifacts_and_reads_without_reexecution():
    service, _, storage, _ = setup()
    context, roots, books = retained_inputs()
    prefix = f'evidence-inputs/{context["qqUuid"]}/package-{context["packageVersion"]}/'
    refs, bookrefs = [], []
    for r in roots:
        path = prefix + r["documentId"] + "/" + r["sha256"]
        storage.sources[path] = r["data"]
        refs.append({k: r[k] for k in ("documentId", "sha256", "sizeBytes", "fileName")} | {"blobPath": path})
    for a in context["assemblies"]:
        bom = a["canonicalBom"]
        path = prefix + bom["documentId"] + "/" + bom["sha256"]
        storage.sources[path] = books[bom["documentId"]]
        bookrefs.append(bom | {"blobPath": path})
    policy = {"producer": "test", "model": "fake", "policyVersion": "package-evidence-foundry.v4", "promptSha256": "d" * 64}
    request = evidence_request(context, refs, bookrefs, policy, source_proof(roots))
    assert service.submit(request)["status"] == "requested"
    result = service.run(request["requestId"], RetentionModel())
    assert result["status"] == "completed"
    assert set(result) == {"requestId", "status", "proposal", "classification", "manufacturingAssessment"}
    replay = RetentionModel()
    assert service.run(request["requestId"], replay) == result and replay.packet is None
    assert service.reconcile_publication(request["requestId"]) == result