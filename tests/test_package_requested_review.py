"""Review rows, obvious links and automatic acceptance (QuickQuote round 2, D3 levels 1 and 2).

Offline: a completed v2 package choice is written directly (decision logic is tested in
test_package_requested_assemblies.py); parsed identity Board rev A.
"""
import hashlib
import json

import pytest

from bom_processor import ProcessingRequest
from bom_processor.bom_package_choice import RESULT_VERSION_V2, PackageChoiceService, choice_request
from bom_processor.package_resolution import PackageResolutionError, requested_links
from bom_processor.package_system_acceptance import (POLICY_V2, VERSION_V2, SystemPackageService,
                                                     requested_bindings)
from test_bom_package_choice import ZIP_ID
from test_package_requested_assemblies import ENE_ID, REQUESTED, SNC_ID
from test_package_survey import archive

ONE_ENE = [{"assembly_id": ENE_ID, "part_number": "ENE010-0810-U2-TK", "revision": "A"}]
ENE_IDENTITY = [{"name": "ENE010-0810-U2-TK", "revision": "A"}]
CALLER = "synthetic-quickquote"


def _linked_choice(store_, request, *, link="confirmed", findings=()):
    """doc-1 primary, doc-2 set aside as redundant, both linked to the first requested assembly."""
    from bom_processor.bom_candidate_choice import value_digest
    from bom_processor.bom_package_choice import _hash, _json_bytes
    from bom_processor.semantic import _norm
    record = choice_request(request)
    rid = record["request_id"]
    assembly_id = request.requested_assemblies[0].assembly_id
    file_ids = {d.document_id: f"F{i}" for i, d in enumerate(request.documents, 1)}
    confirmed = link == "confirmed"
    survey_files = [{"file_id": file_ids[d.document_id], "kind": "spreadsheet_ooxml", "role": "bom",
                     "path": f"package.zip/ENE010-0810-U2 v10p1/{d.document_id}.xlsx",
                     "requested_assembly": {"assembly_id": assembly_id, "ref": "R1", "link": link,
                                            "unconfirmed_reason": None if confirmed else "number_not_found",
                                            "matched": {"text": "ENE010-0810-U2", "source": "path"} if confirmed else None,
                                            "matched_core": "010-0810-U2", "evidence": []}}
                    for d in request.documents]
    survey_files.append({"file_id": "F9", "path": "package.zip/drawing.pdf", "kind": "pdf", "role": "other",
                         "classification_status": "unclassified", "requested_assembly": None})
    documents = [{"document_id": d.document_id, "sha256": d.sha256, "survey_file_ids": [file_ids[d.document_id]],
                  "decision": "excluded" if d.document_id == "doc-2" else "process",
                  "reason": "redundant" if d.document_id == "doc-2" else "primary",
                  "disposition": "redundant" if d.document_id == "doc-2" else "primary",
                  "roles": ["bom"], "assemblies": ["ENE010-0810-U2"],
                  "requested_assembly_ids": [assembly_id] if confirmed else []}
                 for d in request.documents]
    columns = {"sheet": "BOM", "identity_column": 2, "quantity_column": None, "first_row": 2, "last_row": None}
    choice = {"primary": "F1", "automatic_selection_allowed": True,
              "files": [{"file_id": "F1", "disposition": "primary", "columns": columns},
                        {"file_id": "F2", "disposition": "redundant", "columns": columns}]}
    name = request.requested_assemblies[0].part_number
    result = {"version": RESULT_VERSION_V2, "request_id": rid, "model_calls": 2,
              "survey": {"files": survey_files},
              "choices": [{"assembly": name, "assembly_id": assembly_id, "view_sha256": "e" * 64, "choice": choice,
                           "set_aside_digests": {"F2": [value_digest(_norm("R1"))]}}],
              "documents": documents, "findings": list(findings),
              "primary_selections": [{"assembly": name, "primary_document_ids": ["doc-1"],
                                      "excluded_document_ids": ["doc-2"]}],
              "requested_assemblies": record["requested_assemblies"]}
    store_.registry[f"package-choice/{rid}/request.json"] = _json_bytes(record)
    store_.registry[f"package-choice/{rid}/result.json"] = _json_bytes(result)
    store_.registry[f"package-choice/{rid}/completed.json"] = _json_bytes(
        {"request_id": rid, "result_sha256": _hash(result)})
    return PackageChoiceService(store_).status(rid)


def linked_package(requested, **choice_options):
    """Two equivalent parsed BOMs; the choice keeps doc-1; the request carries requested assemblies."""
    from test_package_resolution import package_workbook, request_and_store
    output = package_workbook(assembly=("Board", "A"))
    request, store_ = request_and_store(outputs=[output, output])
    package = archive({"a.csv": b"x"})
    digest = hashlib.sha256(package).hexdigest()
    value = {"job_id": request.job_id, "customer_key": request.customer_key, "parser_key": request.parser_key,
             "rfq_uuid": request.rfq_uuid, "package_version": request.package_version,
             "documents": [{"document_id": d.document_id, "blob_path": d.blob_path, "size_bytes": d.size_bytes,
                            "sha256": d.sha256} for d in request.documents],
             "package_files": [{"document_id": ZIP_ID, "file_name": "package.zip", "size_bytes": len(package),
                                "blob_path": f"evidence-inputs/{request.rfq_uuid}/package-1/{ZIP_ID}/{digest}",
                                "sha256": digest}],
             "requested_assemblies": requested}
    request = ProcessingRequest.from_dict(value)
    # The candidate child covers doc-1 only; doc-2 is set aside by the choice.
    child_path = "candidates/requests/request-1.json"
    child = json.loads(store_.registry[child_path])
    child["source_evidence"] = child["source_evidence"][:1]
    store_.registry[child_path] = json.dumps(child).encode()
    evidence_path = "candidates/request-1/candidate-1/validation/validation-1/evidence.json"
    evidence = json.loads(store_.candidates[evidence_path])
    evidence["fixtures"] = evidence["fixtures"][:1]
    evidence["fixture_count"] = 1
    store_.candidates[evidence_path] = json.dumps(evidence).encode()
    choice = _linked_choice(store_, request, **choice_options)
    service = SystemPackageService(store_)
    plan = service.plan(request, approved_assignments=[], package_choice=choice,
                        pending_assignments=[{"request_id": "request-1", "document_ids": ["doc-1"]}])
    return service, plan


def ready_group(review):
    return next(group for group in review["groups"] if group.get("status") == "ready")


def accept(service, plan, review, identities=ENE_IDENTITY):
    return service.accept_system(plan_id=plan["plan_id"], review_manifest_sha256=review["manifest_sha256"],
                                 assembly_identities=identities, caller_application=CALLER)


def test_review_lists_every_surveyed_file_and_the_obvious_link():
    service, plan = linked_package(ONE_ENE)
    review = service.review(plan["plan_id"])
    assert review["requested_assemblies"] == ONE_ENE
    assert review["package_files"] == [
        {"file_id": "F1", "path": "package.zip/ENE010-0810-U2 v10p1/doc-1.xlsx", "kind": "spreadsheet_ooxml",
         "role": "bom", "classification_status": "classified", "requested_assembly_id": ENE_ID,
         "link": "confirmed", "decision": "primary", "reason": "primary", "document_id": "doc-1"},
        {"file_id": "F2", "path": "package.zip/ENE010-0810-U2 v10p1/doc-2.xlsx", "kind": "spreadsheet_ooxml",
         "role": "bom", "classification_status": "classified", "requested_assembly_id": ENE_ID,
         "link": "confirmed", "decision": "excluded", "reason": "redundant", "document_id": "doc-2"},
        {"file_id": "F9", "path": "package.zip/drawing.pdf", "kind": "pdf", "role": "other",
         "classification_status": "unclassified", "requested_assembly_id": None, "link": "none",
         "decision": "not_parsed", "reason": "other", "document_id": None}]
    assert ready_group(review)["evidence"]["requested_link"] == {
        "assembly_id": ENE_ID, "matched_text": "ENE010-0810-U2",
        "reason": "ENE010-0810-U2 found in the file or folder name"}


def test_system_acceptance_binds_an_obvious_link_and_records_it():
    service, plan = linked_package(ONE_ENE)
    review = service.review(plan["plan_id"])
    # Round 5: the parsed identity (Board) names no requested assembly and the survey link is
    # confirmed, so the review reads the requested assembly as the identity; it binds exactly.
    assert ready_group(review)["evidence"]["assembly_identity"]["name"] == "ene010-0810-u2-tk"
    accepted = accept(service, plan, review)
    assert accepted["version"] == VERSION_V2 and accepted["policy_version"] == POLICY_V2
    assert accepted["bindings"] == [{"assembly_id": ENE_ID, "group_id": ready_group(review)["group_id"],
                                     "mode": "exact"}]
    resolved = service.resolve(plan_id=plan["plan_id"], approval_id=accepted["approval_id"])
    assert [item["document_ids"] for item in resolved["results"]] == [["doc-1"]]
    # Replays re-verify the v2 proof read-only.
    assert service.manifest(plan["plan_id"], accepted["approval_id"]) == resolved


def test_an_exact_parsed_identity_still_binds_exactly():
    service, plan = linked_package([{"assembly_id": ENE_ID, "part_number": "Board", "revision": "A"}])
    review = service.review(plan["plan_id"])
    accepted = accept(service, plan, review, identities=[{"name": "Board", "revision": "A"}])
    assert [item["mode"] for item in accepted["bindings"]] == ["exact"]


@pytest.mark.parametrize("requested, options", [
    (ONE_ENE, {"link": "unconfirmed"}),                                                        # condition 1
    ([{"assembly_id": ENE_ID, "part_number": "ENE010-0810-U2-TK", "revision": "B"}], {}),       # condition 3
])
def test_no_obvious_link_means_a_person_binds(requested, options):
    service, plan = linked_package(requested, **options)
    review = service.review(plan["plan_id"])
    assert "requested_link" not in ready_group(review)["evidence"]
    identities = [{"name": item["part_number"], "revision": item["revision"]} for item in requested]
    with pytest.raises(PackageResolutionError, match="SYSTEM_ASSEMBLY_COVERAGE_INVALID"):
        accept(service, plan, review, identities)


def test_any_package_choice_warning_stops_automatic_acceptance():
    warning = {"code": "SURVEY_BOM_NOT_MAPPED", "severity": "warning", "raised_by": "code", "rationale": "x",
               "document_ids": [], "assemblies": [], "file_paths": ["package.zip/other.xlsx"]}
    service, plan = linked_package(ONE_ENE, findings=[warning])
    review = service.review(plan["plan_id"])
    assert [item["code"] for item in review["package_choice_findings"]] == ["SURVEY_BOM_NOT_MAPPED"]
    assert "requested_link" in ready_group(review)["evidence"]
    with pytest.raises(PackageResolutionError, match="SYSTEM_REVIEW_NOT_QUALIFIED"):
        accept(service, plan, review)


def test_caller_identities_must_be_the_requested_assemblies():
    service, plan = linked_package(ONE_ENE)
    review = service.review(plan["plan_id"])
    with pytest.raises(PackageResolutionError, match="SYSTEM_ASSEMBLY_COVERAGE_INVALID"):
        accept(service, plan, review, identities=[{"name": "Board", "revision": "A"}])


def _group(group_id, documents, name="Board", revision="A", link=None):
    evidence = {"assembly_identity": {"name": name, "revision": revision}}
    if link is not None:
        evidence["requested_link"] = link
    return {"group_id": group_id, "status": "ready", "document_ids": documents, "evidence": evidence}


def _result(document_links):
    files, documents = [], []
    for index, (document_id, links) in enumerate(document_links.items(), 1):
        file_id = f"F{index}"
        record = None if not links else {"assembly_id": links[0], "link": "confirmed",
                                         "matched": {"text": "x", "source": "cell",
                                                     "cell": {"sheet": "BOM", "row": 1, "column": 2}}}
        files.append({"file_id": file_id, "requested_assembly": record})
        documents.append({"document_id": document_id, "survey_file_ids": [file_id], "requested_assembly_ids": links})
    return {"requested_assemblies": REQUESTED, "documents": documents, "survey": {"files": files}}


def test_requested_links_conditions_one_two_and_four():
    result = _result({"d1": [ENE_ID], "d2": [ENE_ID], "d3": [SNC_ID], "d4": []})
    # (4) two ready groups linked to ENE: neither is obvious; SNC alone is, with a cell reason.
    linked = requested_links([_group("g1", ["d1"]), _group("g2", ["d2"]), _group("g3", ["d3"])], result)
    assert [("requested_link" in group["evidence"]) for group in linked] == [False, False, True]
    assert linked[2]["evidence"]["requested_link"]["reason"] == "x found in sheet BOM, row 1"
    # (1) a group mixing a linked and an unlinked document (a brought-back BOM) is not obvious.
    assert "requested_link" not in requested_links([_group("g1", ["d1", "d4"])], result)[0]["evidence"]
    # (2) a parsed name that names another requested assembly contradicts the link.
    assert "requested_link" not in requested_links([_group("g1", ["d1"], name="snc006-002022-01")], result)[0]["evidence"]
    # Unrelated parsed text (a title) does not contradict.
    assert "requested_link" in requested_links([_group("g1", ["d1"], name="energous yondu v10")], result)[0]["evidence"]
    # (3) a contradicting parsed revision.
    assert "requested_link" not in requested_links([_group("g1", ["d1"], revision="v9")], result)[0]["evidence"]


def test_requested_bindings_refuse_disagreement_and_incomplete_coverage():
    requested = [{"assembly_id": ENE_ID, "part_number": "Board", "revision": "A"},
                 {"assembly_id": SNC_ID, "part_number": "Other", "revision": None}]
    identities = sorted([{"name": "board", "revision": "a"}, {"name": "other", "revision": ""}],
                        key=lambda item: (item["name"], item["revision"]))
    link_snc = {"assembly_id": SNC_ID, "reason": "r", "matched_text": "m"}
    bound = requested_bindings([_group("g1", ["d1"]), _group("g2", ["d2"], name="x", revision="", link=link_snc)],
                               requested, identities)
    assert [(item["assembly_id"], item["mode"]) for item in bound] == [(ENE_ID, "exact"), (SNC_ID, "linked")]
    for groups in ([_group("g1", ["d1"], link=link_snc), _group("g2", ["d2"], name="x", link=link_snc)],
                   [_group("g1", ["d1"])],
                   [_group("g1", ["d1"]), _group("g2", ["d2"])]):
        with pytest.raises(PackageResolutionError, match="SYSTEM_ASSEMBLY_COVERAGE_INVALID"):
            requested_bindings(groups, requested, identities)


# --- round 5: identity read as a person would (A037) -------------------------------------------

def _identity_result(links_by_document):
    return {"requested_assemblies": REQUESTED,
            "documents": [{"document_id": doc, "requested_assembly_ids": links} for doc, links in links_by_document.items()]}


def test_identity_from_the_boms_own_assembly_line():
    from bom_processor.package_resolution import requested_identity
    from bom_processor.semantic import AssemblyIdentity
    rows = [{"item_number": "ENE010-0810-U2-TK", "revision": "V10p1-B", "quantity": 0},
            {"item_number": "ENE011-0810-00", "revision": "V10p1-A", "quantity": 1}]
    # A037 ENE: the parsed output names no identity; its Level 0 line names the requested assembly.
    assert requested_identity(_identity_result({"d1": [ENE_ID]}), ["d1"], rows, None) == \
        AssemblyIdentity("ENE010-0810-U2-TK", "V10p1-B")
    # The line alone is enough even without a survey link.
    assert requested_identity(_identity_result({"d1": []}), ["d1"], rows, None) == \
        AssemblyIdentity("ENE010-0810-U2-TK", "V10p1-B")


def test_identity_from_the_confirmed_link_when_no_line_names_an_assembly():
    from bom_processor.package_resolution import requested_identity
    from bom_processor.semantic import AssemblyIdentity
    rows = [{"item_number": "010-002022-01", "revision": None, "quantity": 0},
            {"item_number": "500-002022-00", "revision": None, "quantity": 1}]
    # A037 SNC: the parsed identity is the schematic document number, which names no requested assembly.
    parsed = AssemblyIdentity("010-002022-01", "")
    assert requested_identity(_identity_result({"d1": [SNC_ID]}), ["d1"], rows, parsed) == \
        AssemblyIdentity("SNC006-002022-01-TK", "13")


@pytest.mark.parametrize("links, rows, parsed", [
    # No confirmed link and no line naming an assembly: today's behaviour (parsed or a person).
    ({"d1": []}, [{"item_number": "500-002022-00"}], None),
    # A line names a different requested assembly than the link.
    ({"d1": [SNC_ID]}, [{"item_number": "ENE010-0810-U2-TK"}], None),
    # Lines name two different requested assemblies.
    ({"d1": [ENE_ID]}, [{"item_number": "ENE010-0810-U2-TK"}, {"item_number": "SNC006-002022-01-TK"}], None),
    # The parsed identity names a different requested assembly.
    ({"d1": [ENE_ID]}, [{"item_number": "P-1"}], "SNC006-002022-01"),
    # A clearly different parsed revision.
    ({"d1": [SNC_ID]}, [{"item_number": "P-1"}], ("010-002022-01", "12")),
    # One of two source documents has no confirmed link.
    ({"d1": [SNC_ID], "d2": []}, [{"item_number": "P-1"}], None),
])
def test_identity_is_left_to_today_s_rules_when_not_obvious(links, rows, parsed):
    from bom_processor.package_resolution import requested_identity
    from bom_processor.semantic import AssemblyIdentity
    if isinstance(parsed, str):
        parsed = AssemblyIdentity(parsed, "")
    elif isinstance(parsed, tuple):
        parsed = AssemblyIdentity(*parsed)
    assert requested_identity(_identity_result(links), sorted(links), rows, parsed) is None
