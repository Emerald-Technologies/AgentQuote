"""Offline source-grounded model hints/publication, no live model or customer data."""
import pytest

from bom_processor.package_evidence_contracts import PackageEvidenceError, json_bytes, object_hash, utf16_units
from bom_processor.package_evidence_proposal import produce_proposal
from test_package_evidence_proposal import inputs, E, F, G
from test_package_evidence_inventory import root
from test_package_part_classification import ClassifiedModel
from test_package_evidence_jobs import setup
from bom_processor.package_evidence_jobs import evidence_request


def assessed_inputs():
    context, roots, books = inputs()
    # QQ imports a distinct output document per assembly even for equal bytes.
    old = context["assemblies"][0]["canonicalBom"]["documentId"]
    new = "12345678-1234-4234-8234-123456789099"
    context["assemblies"][0]["canonicalBom"]["documentId"] = new
    books[new] = books[old]
    roots = [root(b"ASSEMBLY DRAWING\nPart Number: ASM\nRevision: A", "assembly.txt", E),
             root(b"DETAIL DRAWING\nPart Number: PCB\nRevision: A", "part.txt", F),
             root(b"Retain unknown evidence", "other.txt", G)]
    return context, roots, books


class AssessedModel(ClassifiedModel):
    def propose(self, packet):
        out = super().propose(packet)
        sources = {s["evidenceId"]: s for s in packet["manufacturingSources"]}
        source = next(iter(sources.values()))
        def citation(s):
            return {"evidenceId": s["evidenceId"], "page": None, "start": 0,
                    "end": utf16_units(s["text"]), "text": s["text"]}
        out["manufacturing"] = {"requirements": [
            {"subject": identity, "requiredRoles": [], "determination": "unknown",
             "rationale": "Offline unresolved requirements", "citations": [citation(source)], "conflicts": []}
            for identity in [a["assemblyUuid"] for a in packet["context"]["assemblies"]]
                + [o["occurrenceId"] for o in packet["occurrences"]]],
            "relationships": [{**{k: a[k] for k in ("evidenceId", "occurrenceIds", "assemblyUuids", "scope")},
                "role": "assembly_drawing", "sharing": "unknown" if a["scope"] == "shared" else "not_shared",
                "citations": [citation(sources[a["evidenceId"]])], "conflicts": []} for a in out["associations"]]}
        return out


def produce(model=None):
    context, roots, books = assessed_inputs()
    return produce_proposal(context, roots, books, model=model or AssessedModel(), producer="offline-test",
        model_name="fake-model", policy_version="package-evidence-foundry.v3", prompt_sha256="d" * 64,
        run_id="test-run", include_classification=True, include_manufacturing=True)


def test_complete_source_assessment_hash_coverage_and_code_owned_relationship_ids():
    result = produce()
    assessment = result["manufacturingAssessment"]
    assert assessment["assessmentSha256"] == object_hash({k: v for k, v in assessment.items() if k != "assessmentSha256"})
    assert assessment["classificationSha256"] == result["classification"]["classificationSha256"]
    assert len(assessment["requirements"]) == 6
    assert {r["associationId"] for r in assessment["relationships"]} == {a["associationId"] for a in result["proposal"]["associations"]}


@pytest.mark.parametrize("change", [
    lambda m: m["requirements"].pop(),
    lambda m: m["relationships"].pop(),
    lambda m: m["requirements"][0]["citations"][0].update(text="Invented"),
    lambda m: m["requirements"][0].update(subject="foreign"),
])
def test_missing_foreign_or_invented_sources_fail(change):
    class Invalid(AssessedModel):
        def propose(self, packet):
            out = super().propose(packet)
            change(out["manufacturing"])
            return out
    with pytest.raises(PackageEvidenceError):
        produce(Invalid())


def _cited(change):
    class Changed(AssessedModel):
        def propose(self, packet):
            out = super().propose(packet)
            change(out["manufacturing"], packet)
            return out
    return produce(Changed())


def test_model_offsets_are_ignored_and_code_locates_exact_quote():
    # A029 retry ce0346b6: exact quote, model-counted offsets wrong. Code now derives them.
    def wrong_offsets(m, packet):
        source = packet["manufacturingSources"][0]
        cite = m["requirements"][0]["citations"][0]
        cite.update(text=source["text"][5:15], start=999, end=1000)
        m["requirements"][1]["citations"][0] = {"evidenceId": source["evidenceId"], "page": None, "text": source["text"][5:15]}
    result = _cited(wrong_offsets)
    for requirement in result["manufacturingAssessment"]["requirements"][:2]:
        cite = requirement["citations"][0]
        assert (cite["start"], cite["end"]) == (5, 15)
        assert set(cite) == {"evidenceId", "page", "start", "end", "text"}


def test_located_offsets_are_utf16_units():
    from bom_processor.package_manufacturing_assessment import assessment_artifact
    graph = produce()
    proposal, classification = graph["proposal"], graph["classification"]
    hints = {"requirements": [{**r, "citations": [{"evidenceId": "e" * 64, "page": None, "text": "Rev B"}]}
                              for r in graph["manufacturingAssessment"]["requirements"]],
             "relationships": []}
    proposal = {**proposal, "associations": []}
    source = [{"evidenceId": "e" * 64, "page": None, "text": "\U0001F600 x Rev B", "readerVersion": "quickquote-complete-text.v1"}]
    artifact = assessment_artifact(proposal, classification, source, hints)
    cite = artifact["requirements"][0]["citations"][0]
    # The emoji is 2 UTF16 units, so "Rev B" starts at unit 5 (JavaScript slice), not code point 4.
    assert (cite["start"], cite["end"]) == (5, 10)
    assert "\U0001F600 x Rev B".encode("utf-16-le")[cite["start"] * 2:cite["end"] * 2].decode("utf-16-le") == "Rev B"


@pytest.mark.parametrize("change", [
    # Text from a different file than the one cited.
    lambda m, p: m["requirements"][0]["citations"][0].update(
        evidenceId=next(s["evidenceId"] for s in p["manufacturingSources"]
                        if s["evidenceId"] != m["requirements"][0]["citations"][0]["evidenceId"])),
    lambda m, p: m["requirements"][0]["citations"][0].update(note="extra"),
    lambda m, p: m["requirements"][0]["citations"][0].pop("text"),
    lambda m, p: m["requirements"][0]["citations"].append(dict(m["requirements"][0]["citations"][0], start=1)),
])
def test_foreign_extra_missing_or_duplicate_citations_still_fail(change):
    with pytest.raises(PackageEvidenceError):
        _cited(change)


def assessed_service():
    service, original, storage, _ = setup()
    context, roots, books = assessed_inputs()
    prefix = f"evidence-inputs/{context['qqUuid']}/package-{context['packageVersion']}/"
    refs = []
    for r in roots:
        location = prefix + r["documentId"] + "/" + r["sha256"]
        storage.sources[location] = r["data"]
        refs.append({k: r[k] for k in ("documentId", "sha256", "sizeBytes", "fileName")} | {"blobPath": location})
    bookrefs = []
    for document, data in books.items():
        bom = next(a["canonicalBom"] for a in context["assemblies"] if a["canonicalBom"]["documentId"] == document)
        location = prefix + document + "/" + bom["sha256"]
        storage.sources[location] = data
        bookrefs.append({**bom, "blobPath": location})
    request = evidence_request(context, refs, bookrefs,
        {**original["policy"], "policyVersion": "package-evidence-foundry.v3"})
    service.submit(request)
    return service, request, storage


def test_v3_job_publishes_three_artifacts_and_replays_without_model():
    service, request, storage = assessed_service()
    result = service.run(request["requestId"], AssessedModel())
    assert result["status"] == "completed"
    assert result["manufacturingAssessment"]["runId"] == request["requestId"]
    second = AssessedModel()
    assert service.run(request["requestId"], second) == result and second.packet is None
    marker = service._read(request["requestId"], "completed")
    assert "assessmentArtifactSha256" in marker
    path = service._path(request["requestId"], "manufacturing-assessment")
    artifact = result["manufacturingAssessment"]
    artifact["requirements"][0]["rationale"] = "Changed"
    storage.registry[path] = json_bytes(artifact)
    with pytest.raises(PackageEvidenceError, match="ASSESSMENT_RESULT_BINDING"):
        service.status(request["requestId"])


def test_interrupted_assessment_publication_cannot_complete_or_reexecute():
    service, request, storage = assessed_service()
    original = storage.upload_registry_immutable
    def interrupted(path, data, content_type):
        if path.endswith("/manufacturing-assessment.json"):
            raise RuntimeError("interrupted")
        original(path, data, content_type)
    storage.upload_registry_immutable = interrupted
    with pytest.raises(PackageEvidenceError, match="PUBLICATION_UNKNOWN"):
        service.run(request["requestId"], AssessedModel())
    storage.upload_registry_immutable = original
    with pytest.raises(PackageEvidenceError, match="ASSESSMENT_RESULT_MISSING"):
        service.reconcile_publication(request["requestId"])
    second = AssessedModel()
    assert service.run(request["requestId"], second)["status"] == "running" and second.packet is None