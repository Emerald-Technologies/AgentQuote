"""Agent document-role proposals: byte verification and author gating (offline, synthetic)."""
import copy
import json

import pytest

from bom_processor.candidate_authoring import AuthoringError, CandidateAuthoringService
from bom_processor.document_role import (ROLE_DEFINITIONS, DocumentRoleError,
                                         verify_document_role)
from test_source_assembly_association import roles, setup, workbook


def _request_v2(store):
    path = "candidates/requests/xlsx-demo.json"
    request = json.loads(store.registry[path])
    request["version"] = "candidate-request.v2"
    store.registry[path] = json.dumps(request).encode()


class Model:
    def __init__(self, document_roles, associations):
        self.reply = {"parser_code": "unused", "document_roles": document_roles,
                      "assembly_associations": associations}

    def generate_bundle(self, *_):
        return copy.deepcopy(self.reply)


def test_definitions_are_broad_guidance_for_bom_and_mpi():
    text = ROLE_DEFINITIONS.casefold()
    for phrase in ("bill of materials", "reference designators", "work instruction", "mpi",
                   "headers", "not proof", "warning finding"):
        assert phrase in text


def test_cited_values_are_verified_against_source_bytes():
    store, sources, _, _ = setup()
    source = store.sources[sources[0]["blob_path"]]
    proposal = roles(store, sources[:1])[sources[0]["sha256"]]
    verified = verify_document_role(source, ".xlsx", proposal)
    assert verified["role"] == "bom" and verified["source_values_verified"] is True
    for mutate, code in [
            (lambda p: p["evidence"][0].update(value="Invented"), "DOCUMENT_ROLE_SOURCE_VALUE_CHANGED"),
            (lambda p: p["evidence"][0]["cell"].update(row=99), "DOCUMENT_ROLE_SOURCE_VALUE_CHANGED"),
            (lambda p: p.update(role="drawing"), "DOCUMENT_ROLE_BINDING_INVALID"),
            (lambda p: p.update(evidence=[]), "DOCUMENT_ROLE_BINDING_INVALID"),
            (lambda p: p.update(source_sha256="0" * 64), "DOCUMENT_ROLE_BINDING_INVALID"),
            (lambda p: p["findings"].append({"code": "X", "severity": "fatal", "rationale": "r"}),
             "DOCUMENT_ROLE_FINDING_INVALID")]:
        changed = copy.deepcopy(proposal)
        mutate(changed)
        with pytest.raises(DocumentRoleError, match=code):
            verify_document_role(source, ".xlsx", changed)


@pytest.mark.parametrize("role,code", [("work_instruction", "SOURCE_NOT_BOM"), ("other", "SOURCE_NOT_BOM")])
def test_non_bom_source_stops_before_parser_and_records_role(role, code):
    store, sources, associations, _ = setup()
    _request_v2(store)
    proposals = roles(store, sources, role=role)
    before = set(store.candidates)
    with pytest.raises(AuthoringError, match=code):
        CandidateAuthoringService("xlsx-demo", store, Model(proposals, associations),
                                  source_associations=True).author()
    written = sorted(set(store.candidates) - before)
    assert len(written) == 1 and "/document-roles/" in written[0]
    record = json.loads(store.candidates[written[0]])
    assert {item["role"] for item in record.values()} == {role}


def test_mixed_roles_and_blocking_findings_require_review():
    store, sources, associations, _ = setup()
    _request_v2(store)
    proposals = roles(store, sources)
    proposals[sources[-1]["sha256"]]["role"] = "work_instruction"
    with pytest.raises(AuthoringError, match="SOURCE_ROLES_MIXED_REVIEW_REQUIRED"):
        CandidateAuthoringService("xlsx-demo", store, Model(proposals, associations),
                                  source_associations=True).author()
    proposals = roles(store, sources)
    proposals[sources[0]["sha256"]]["findings"] = [
        {"code": "ROLE_UNCLEAR", "severity": "blocking", "rationale": "Cannot tell."}]
    with pytest.raises(AuthoringError, match="DOCUMENT_ROLE_REVIEW_REQUIRED"):
        CandidateAuthoringService("xlsx-demo", store, Model(proposals, associations),
                                  source_associations=True).author()


def test_role_coverage_must_match_every_source():
    store, sources, associations, _ = setup()
    _request_v2(store)
    proposals = roles(store, sources)
    proposals.pop(sources[0]["sha256"])
    with pytest.raises(AuthoringError, match="DOCUMENT_ROLE_COVERAGE_INVALID"):
        CandidateAuthoringService("xlsx-demo", store, Model(proposals, associations),
                                  source_associations=True).author()


def test_mpi_style_workbook_role_is_the_agents_choice_and_verifiable():
    # Layout sheet: assembly/revision block plus numbered steps; no parts table.
    source = workbook([["Work Instruction", "ASM-9"], ["Revision", "C"],
                       ["Step", "Operation", "Notes"], ["10", "Apply solder paste", "Stencil 5 mil"],
                       ["20", "Place components", "Refer to drawing"], ["30", "Reflow", "Profile B"]])
    from hashlib import sha256
    from bom_processor.document_role import VERSION
    proposal = {"version": VERSION, "source_sha256": sha256(source).hexdigest(), "role": "work_instruction",
                "rationale": "Ordered build steps with process notes; no parts list.",
                "evidence": [{"value": "Step", "cell": {"sheet": "Customer", "row": 3, "column": 1}},
                             {"value": "Apply solder paste", "cell": {"sheet": "Customer", "row": 4, "column": 2}}],
                "findings": []}
    assert verify_document_role(source, ".xlsx", proposal)["role"] == "work_instruction"
