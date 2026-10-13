"""Requested assemblies in the package choice (QuickQuote round 2, Nick 2026-10-10). Offline, stub agent.

The package mirrors A035: two BOMs for ENE010-0810-U2-TK in a folder named with the customer number,
one SNC BOM, a BOM of a sibling assembly (ENE011) that was not requested, and a work instruction.
"""
import copy
import hashlib
import json

import pytest

from bom_processor import ProcessingError, ProcessingRequest
from bom_processor.bom_candidate_choice import REPLY_VERSION as CHOICE_VERSION
from bom_processor.bom_package_choice import (REQUEST_VERSION, REQUEST_VERSION_V2, RESULT_VERSION_V2,
                                              PackageChoiceService, apply_reopen, choice_request,
                                              excluded_assignments)
from bom_processor.package_survey import SURVEY_INSTRUCTIONS, SURVEY_INSTRUCTIONS_V2
from bom_processor import InMemoryStorage
from test_bom_package_choice import (HEADER, RFQ, ZIP_ID, Starter, cell, completed_service, lines,
                                     package_path, source_path)
from test_package_survey import archive, xlsx

ENE_ID = "aaaaaaaa-1111-4111-8111-111111111111"
SNC_ID = "bbbbbbbb-2222-4222-8222-222222222222"
REQUESTED = [{"assembly_id": ENE_ID, "part_number": "ENE010-0810-U2-TK", "revision": "V10p1-B"},
             {"assembly_id": SNC_ID, "part_number": "SNC006-002022-01-TK", "revision": "13"}]

ENE_MOD = xlsx({"BOM": [["Energous Yondu V10 013-0810-00p1", "v10p1"], HEADER] + lines(6)})
ENE_PLAIN = xlsx({"BOM": [["Energous Yondu V10 013-0810-00p1", "v10p1"], HEADER] + lines(5)})
SNC = xlsx({"BOM": [["Assembly", "006-002022-01"], HEADER] + lines(4, start=50)})
SIBLING = xlsx({"BOM": [["Assembly", "011-0810-00"], HEADER] + lines(3, start=90)})
MPI = xlsx({"MPI": [["Manufacturing Process Instruction"], ["Assembly:006-002022-01"], ["Step", "Op"], [10, "Paste"]]})
FILES = {"ENE010-0810-U2 v10p1/ENE MOD BOM.xlsx": ENE_MOD, "ENE010-0810-U2 v10p1/ENE BOM.xlsx": ENE_PLAIN,
         "SNC006-002022-01-TK/SNC BOM.xlsx": SNC, "ENE011-0810-00/SIB BOM.xlsx": SIBLING,
         "SNC006-002022-01-TK/SNC MPI.xlsx": MPI}
PACKAGE = archive(FILES)
DOCS = {"doc-ene-mod": ENE_MOD, "doc-ene": ENE_PLAIN, "doc-snc": SNC, "doc-sib": SIBLING, "doc-mpi": MPI}


def request_dict(requested=REQUESTED, docs=DOCS, package=PACKAGE):
    value = {"job_id": "job-1", "customer_key": "cust", "parser_key": "Parser1", "rfq_uuid": RFQ,
             "package_version": "1",
             "documents": [{"document_id": doc, "blob_path": source_path(doc), "size_bytes": len(data),
                            "sha256": hashlib.sha256(data).hexdigest()} for doc, data in sorted(docs.items())],
             "package_files": [{"document_id": ZIP_ID, "file_name": "package.zip", "blob_path": package_path(package),
                                "size_bytes": len(package), "sha256": hashlib.sha256(package).hexdigest()}]}
    if requested is not None:
        value["requested_assemblies"] = copy.deepcopy(requested)
    return value


def path_item(value):
    return {"source": "path", "value": value}


class LinkingAgent:
    """Survey v2 replies linked by folder; the choice keeps the MOD BOM and sets the plain one aside."""

    def __init__(self, links=None, revision=None, link_evidence=None):
        # links: file name -> (ref, cited path text) overrides; revision: cited revision cell text;
        # link_evidence: file name -> raw evidence items for the link (to send refused citations).
        self.links = links or {}
        self.revision = revision
        self.link_evidence = link_evidence or {}
        self.instructions, self.texts, self.choice_calls = [], [], 0

    def __call__(self, instructions, text):
        body, _ = json.JSONDecoder().raw_decode(text)
        if "classify" not in body:
            self.choice_calls += 1
            files = []
            for entry in body["files"]:
                name = entry["path"].rsplit("/", 1)[-1]
                files.append({"file_id": entry["id"], "disposition": "primary" if "MOD" in name else "redundant",
                              "rationale": "Stub.", "evidence": [cell("BOM", 2, 2, "Part Number")],
                              "columns": {"sheet": "BOM", "identity_column": 2, "quantity_column": 5,
                                          "first_row": 3, "last_row": None}})
            primary = next(item["file_id"] for item in files if item["disposition"] == "primary")
            return {"version": CHOICE_VERSION, "primary": primary, "files": files,
                    "rationale": "Stub choice.", "findings": []}
        self.instructions.append(instructions)
        self.texts.append(text)
        files = []
        for entry in body["files"]:
            folder, name = entry["path"].rsplit("/", 2)[-2:]
            sheet = entry["sheets"][0]["name"]
            role = "work_instruction" if "MPI" in name else "bom"
            evidence = [cell(sheet, 1, 1, "Manufacturing Process Instruction")] if role == "work_instruction" \
                else [cell(sheet, 2, 2, "Part Number")]
            if folder.startswith("ENE010"):
                link = ("R1", "ENE010-0810-U2")
            elif folder.startswith("SNC"):
                link = ("R2", "SNC006-002022-01-TK")
            else:
                link = None
            link = self.links.get(name, link)
            revision = [cell(sheet, 1, 2, self.revision)] if self.revision and folder.startswith("ENE010") else []
            files.append({"file_id": entry["id"], "role": role, "possibly_shared": False, "rationale": "Stub.",
                          "findings": [], "evidence": evidence,
                          "assembly": {"name": [path_item(folder.split(" ")[0])], "revision": revision},
                          "requested_assembly": None if link is None else
                          {"ref": link[0], "matched_core": "core",
                           "evidence": self.link_evidence.get(name, [path_item(link[1])])}})
        return {"version": "package-survey-reply.v2", "files": files, "findings": []}


def run(agent=None, requested=REQUESTED, docs=DOCS, package=PACKAGE):
    sources = {source_path(doc): data for doc, data in docs.items()}
    sources[package_path(package)] = package
    storage = InMemoryStorage(sources=sources)
    service = PackageChoiceService(storage, Starter())
    request = ProcessingRequest.from_dict(request_dict(requested, docs, package))
    state = service.ensure(request)
    agent = agent or LinkingAgent()
    return service, request, service.run(state["request_id"], agent), agent


def decisions(state):
    return {item["document_id"]: (item["decision"], item["reason"]) for item in state["result"]["documents"]}


def codes(state):
    return {item["code"]: item for item in state["result"]["findings"]}


# --- request contract -------------------------------------------------------------------------

def test_requested_assemblies_are_parsed_and_bound_to_both_fingerprints():
    request = ProcessingRequest.from_dict(request_dict())
    plain = ProcessingRequest.from_dict(request_dict(requested=None))
    assert [item.part_number for item in request.requested_assemblies] == ["ENE010-0810-U2-TK", "SNC006-002022-01-TK"]
    assert plain.requested_assemblies is None
    assert request.fingerprint() != plain.fingerprint()
    assert choice_request(plain)["version"] == REQUEST_VERSION and "requested_assemblies" not in choice_request(plain)
    record = choice_request(request)
    assert record["version"] == REQUEST_VERSION_V2 and record["requested_assemblies"] == REQUESTED
    assert record["request_id"] != choice_request(plain)["request_id"]
    renamed = request_dict()
    renamed["requested_assemblies"][1]["revision"] = None
    assert ProcessingRequest.from_dict(renamed).fingerprint() != request.fingerprint()


GOLDEN = "d64f0b5446b034120dba54e93aa00589f3e6d27d6f0c27d01e1f3861db0e23f2"


def test_requested_assemblies_fingerprint_golden_vector():
    # Shared with QuickQuote tests/unit/agentquote-requested-assemblies.test.ts: both languages must agree.
    value = {"job_id": "qq-0123456789abcdef0123456789abcdef", "customer_key": "internal-abc",
             "parser_key": "internal-abc", "rfq_uuid": "11111111-2222-4333-8444-555555555555",
             "package_version": "1",
             "documents": [{"document_id": "doc-1", "size_bytes": 10, "sha256": "a" * 64,
                            "blob_path": "internal-abc/rfq-11111111-2222-4333-8444-555555555555/package-1/original-boms/doc-1-bom.xlsx"}],
             "package_files": [{"document_id": "2222abcd-3333-4444-8555-666666666666", "file_name": "RFQ PACKAGE.zip",
                                "size_bytes": 20, "sha256": "b" * 64,
                                "blob_path": "evidence-inputs/11111111-2222-4333-8444-555555555555/package-1/2222abcd-3333-4444-8555-666666666666/" + "b" * 64}],
             "requested_assemblies": [{"assembly_id": ENE_ID, "part_number": "ENE010-0810-U2-TK", "revision": "V10p1-B"},
                                      {"assembly_id": SNC_ID, "part_number": "SNC006-002022-01-TK", "revision": None}]}
    assert ProcessingRequest.from_dict(value).fingerprint() == GOLDEN


@pytest.mark.parametrize("change", [
    lambda v: v.pop("package_files"),
    lambda v: v.update(requested_assemblies=[]),
    lambda v: v.update(requested_assemblies="x"),
    lambda v: v["requested_assemblies"].reverse(),
    lambda v: v["requested_assemblies"].append(copy.deepcopy(v["requested_assemblies"][0])),
    lambda v: v["requested_assemblies"][0].update(extra=1),
    lambda v: v["requested_assemblies"][0].pop("revision"),
    lambda v: v["requested_assemblies"][0].update(assembly_id=ENE_ID.upper()),
    lambda v: v["requested_assemblies"][0].update(assembly_id="not-a-uuid"),
    lambda v: v["requested_assemblies"][0].update(part_number=""),
    lambda v: v["requested_assemblies"][0].update(part_number=" ENE010"),
    lambda v: v["requested_assemblies"][0].update(part_number="x" * 101),
    lambda v: v["requested_assemblies"][0].update(part_number="A\nB"),
    lambda v: v["requested_assemblies"][0].update(revision=""),
    lambda v: v["requested_assemblies"][0].update(revision=13),
    lambda v: v["requested_assemblies"][0].update(revision="x" * 51),
    lambda v: v.update(requested_assemblies=[
        {"assembly_id": f"{index:08x}-1111-4111-8111-111111111111", "part_number": f"P{index}", "revision": None}
        for index in range(51)]),
])
def test_requested_assemblies_are_strictly_validated(change):
    value = request_dict()
    change(value)
    with pytest.raises(ProcessingError):
        ProcessingRequest.from_dict(value)


# --- survey and choice -------------------------------------------------------------------------

def test_survey_v2_shows_requested_assemblies_by_ref_and_states_the_reading_rules():
    _, _, done, agent = run()
    assert done["status"] == "completed"
    assert agent.instructions and all(text == SURVEY_INSTRUCTIONS_V2 for text in agent.instructions)
    assert SURVEY_INSTRUCTIONS_V2 != SURVEY_INSTRUCTIONS and "package-survey-reply.v2" in SURVEY_INSTRUCTIONS_V2
    for phrase in ("customer-code prefix", "core number", "never link by elimination", "requested_assembly"):
        assert phrase in SURVEY_INSTRUCTIONS_V2
    body, _ = json.JSONDecoder().raw_decode(agent.texts[0])
    assert body["version"] == "package-survey-view.v2"
    assert body["requested_assemblies"] == [
        {"ref": "R1", "part_number": "ENE010-0810-U2-TK", "revision": "V10p1-B"},
        {"ref": "R2", "part_number": "SNC006-002022-01-TK", "revision": "13"}]
    assert ENE_ID not in agent.texts[0] and SNC_ID not in agent.texts[0]


def test_both_ene_boms_link_and_the_primary_choice_runs_across_them():
    _, _, done, agent = run()
    result = done["result"]
    assert result["version"] == RESULT_VERSION_V2 and result["requested_assemblies"] == REQUESTED
    assert agent.choice_calls == 1
    assert [entry["assembly_id"] for entry in result["choices"]] == [ENE_ID]
    assert decisions(done) == {
        "doc-ene-mod": ("process", "primary"), "doc-ene": ("excluded", "redundant"),
        "doc-snc": ("process", "only_bom"), "doc-sib": ("excluded", "not_mapped"),
        "doc-mpi": ("excluded", "work_instruction")}
    links = {item["document_id"]: item["requested_assembly_ids"] for item in result["documents"]}
    assert links["doc-ene-mod"] == [ENE_ID] and links["doc-snc"] == [SNC_ID] and links["doc-sib"] == []
    ene = next(item for item in result["survey"]["files"] if item["path"].endswith("ENE BOM.xlsx"))
    assert ene["requested_assembly"]["link"] == "confirmed"
    assert ene["requested_assembly"]["matched"] == {"text": "ENE010-0810-U2", "source": "path"}
    found = codes(done)
    assert set(found) == {"SURVEY_BOM_NOT_MAPPED"}
    assert found["SURVEY_BOM_NOT_MAPPED"]["document_ids"] == ["doc-sib"]
    assert found["SURVEY_BOM_NOT_MAPPED"]["file_paths"] == ["package.zip/ENE011-0810-00/SIB BOM.xlsx"]


def test_a_sibling_number_linked_by_the_agent_is_unconfirmed_and_set_aside():
    agent = LinkingAgent(links={"SIB BOM.xlsx": ("R1", "ENE011-0810-00")})
    _, _, done, _ = run(agent)
    assert decisions(done)["doc-sib"] == ("excluded", "not_mapped")
    sib = next(item for item in done["result"]["survey"]["files"] if item["path"].endswith("SIB BOM.xlsx"))
    assert sib["requested_assembly"]["link"] == "unconfirmed"
    assert sib["requested_assembly"]["unconfirmed_reason"] == "number_not_found"
    assert any(item["code"] == "SURVEY_MAPPING_UNCONFIRMED" for item in sib["findings"])
    assert set(codes(done)) == {"SURVEY_MAPPING_UNCONFIRMED"}


def test_a_contradicting_revision_leaves_the_link_unconfirmed():
    agent = LinkingAgent(revision="v9")
    ene_mod = xlsx({"BOM": [["Energous Yondu V10 013-0810-00p1", "v9"], HEADER] + lines(6)})
    ene_plain = xlsx({"BOM": [["Energous Yondu V10 013-0810-00p1", "v9"], HEADER] + lines(5)})
    files = dict(FILES)
    files["ENE010-0810-U2 v10p1/ENE MOD BOM.xlsx"] = ene_mod
    files["ENE010-0810-U2 v10p1/ENE BOM.xlsx"] = ene_plain
    docs = dict(DOCS, **{"doc-ene-mod": ene_mod, "doc-ene": ene_plain})
    _, _, done, _ = run(agent, docs=docs, package=archive(files))
    assert decisions(done)["doc-ene-mod"] == ("excluded", "not_mapped")
    assert decisions(done)["doc-ene"] == ("excluded", "not_mapped")
    ene = next(item for item in done["result"]["survey"]["files"] if item["path"].endswith("ENE BOM.xlsx"))
    assert ene["requested_assembly"]["unconfirmed_reason"] == "revision_differs"
    assert agent.choice_calls == 0


def test_a_set_aside_unmapped_bom_can_be_brought_back():
    _, _, done, _ = run()
    reopened = apply_reopen(done, ["doc-sib"])
    assert decisions(reopened)["doc-sib"] == ("process", "reopened")
    assert {item["document_ids"][0]: item["reason"] for item in excluded_assignments(done)}["doc-sib"] == "not_mapped"


def test_no_bom_linked_to_any_requested_assembly_fails_with_its_own_code():
    agent = LinkingAgent(links={name: None for name in ("ENE MOD BOM.xlsx", "ENE BOM.xlsx", "SNC BOM.xlsx")})
    _, _, done, _ = run(agent)
    assert done == {"request_id": done["request_id"], "status": "failed", "code": "PACKAGE_NO_REQUESTED_BOM"}


def test_a_stored_v1_request_and_result_are_still_read():
    _, service, _, done = completed_service()
    assert done["status"] == "completed" and done["result"]["version"] == "bom-package-choice.v1"
    assert service.status(done["request_id"])["result"]["version"] == "bom-package-choice.v1"


# --- round 3: a refused link citation drops only the link (A036) -----------------------------------

def test_a_file_name_cited_as_a_cell_keeps_the_bom_and_the_choice_runs():
    # A036: the plain ENE BOM's link cited its file name as a cell value; that blocked the whole choice.
    bad = [cell("BOM", 1, 4, "ENE BOM.xlsx")]
    _, _, done, agent = run(LinkingAgent(link_evidence={"ENE BOM.xlsx": bad}))
    assert done["status"] == "completed" and agent.choice_calls == 1
    assert decisions(done)["doc-ene"] == ("excluded", "redundant")
    ene = next(item for item in done["result"]["survey"]["files"] if item["path"].endswith("ENE BOM.xlsx"))
    assert ene["role"] == "bom" and "classification_status" not in ene
    # Confirmed from the file's own folder, cited in its provisional assembly name.
    assert ene["requested_assembly"]["link"] == "confirmed" and ene["requested_assembly"]["evidence"] == []
    assert ene["refused_citations"] == [{"field": "requested_assembly", "failure_code": "SURVEY_EVIDENCE_CELL_NOT_SHOWN",
                                         "source": "cell", "value": "ENE BOM.xlsx",
                                         "cell": {"sheet": "BOM", "row": 1, "column": 4}}]
    assert any(item["code"] == "SURVEY_LINK_CITATION_REFUSED" for item in ene["findings"])
    assert not done["result"]["survey"]["has_blocking_findings"]


def test_board_files_citing_another_folder_stay_classified_with_an_unconfirmed_link():
    # A036: the SNC board package's files cited the sibling assembly folder, not their own path.
    _, _, done, _ = run(LinkingAgent(links={"SIB BOM.xlsx": ("R2", "SNC006-002022-01-TK")}))
    sib = next(item for item in done["result"]["survey"]["files"] if item["path"].endswith("SIB BOM.xlsx"))
    assert "classification_status" not in sib and sib["role"] == "bom"
    assert sib["requested_assembly"]["link"] == "unconfirmed"
    assert sib["requested_assembly"]["unconfirmed_reason"] == "citation_refused"
    assert sib["refused_citations"][0]["failure_code"] == "SURVEY_EVIDENCE_NOT_IN_PATH"
    assert decisions(done)["doc-sib"] == ("excluded", "not_mapped")
    assert set(codes(done)) == {"SURVEY_MAPPING_UNCONFIRMED"}


def test_an_unknown_ref_drops_the_link_but_keeps_the_file():
    _, _, done, _ = run(LinkingAgent(links={"SIB BOM.xlsx": ("R9", "ENE011-0810-00")}))
    sib = next(item for item in done["result"]["survey"]["files"] if item["path"].endswith("SIB BOM.xlsx"))
    assert sib["requested_assembly"] is None and sib["role"] == "bom"
    assert sib["refused_citations"] == [{"field": "requested_assembly", "failure_code": "SURVEY_REQUESTED_ASSEMBLY_INVALID"}]


def test_round3_instructions_state_the_citation_rule_and_board_files():
    for phrase in ("this file's own path", "never as a cell value", "bare board (PCB)", "Gerber, drill"):
        assert phrase in SURVEY_INSTRUCTIONS_V2
    assert "011-0810-00" not in SURVEY_INSTRUCTIONS_V2


def test_a_failed_call_records_its_exception_type_and_a_cut_off_reply_is_named():
    from bom_processor.package_survey import SurveyError, build_view, expand_package
    from bom_processor.package_survey_run import foundry_caller, survey_view

    class Cut:
        finish_reason = "length"
        text = '{"version": "package-survey-reply.v2", "files": ['

    class Factory:
        def __init__(self, **_):
            pass

        async def run(self, *_args, **_kwargs):
            return Cut()

    with pytest.raises(SurveyError, match="SURVEY_REPLY_TRUNCATED"):
        foundry_caller(agent_factory=Factory)("instructions", "text")

    def down(_instructions, _text):
        raise TimeoutError("private transport detail")

    survey = survey_view(build_view(expand_package([("package.zip", PACKAGE)]), REQUESTED), down)
    attempts = next(item for item in survey["findings"] if item["code"] == "SURVEY_BATCH_FAILED")["attempt_failures"]
    assert attempts == [{"failure_code": "SURVEY_CALL_FAILED", "file_id": None, "exception_type": "TimeoutError"}] * 2
    assert "private transport detail" not in json.dumps(survey)


# --- round 6: shorter survey replies (A038) -------------------------------------------------------

def test_survey_batches_hold_at_most_twenty_files_and_ask_for_short_rationales():
    from bom_processor.package_survey import BATCH_FILE_LIMIT, PackageFile, build_view, plan_batches
    files = [PackageFile(("package.zip", f"drawings/D-{n:03}.pdf"), f"{n:064x}", 10, b"%PDF")
             for n in range(1, 46)]
    batches = plan_batches(build_view(files, REQUESTED))
    assert BATCH_FILE_LIMIT == 20 and [len(batch.file_ids) for batch in batches] == [20, 20, 5]
    assert "rationale is at most 200 characters" in SURVEY_INSTRUCTIONS_V2


def test_an_incomplete_json_reply_from_the_model_library_is_named_without_its_text():
    from bom_processor.package_survey import SurveyError
    from bom_processor.package_survey_run import foundry_caller

    def factory_raising(error):
        class Factory:
            def __init__(self, **_):
                pass

            async def run(self, *_args, **_kwargs):
                raise error
        return Factory

    with pytest.raises(SurveyError, match="SURVEY_REPLY_NOT_JSON") as caught:
        foundry_caller(agent_factory=factory_raising(ValueError('Response text is not valid JSON: "private reply"')))("i", "t")
    assert "private reply" not in str(caught.value) and caught.value.__cause__ is None
    with pytest.raises(ValueError, match="other"):
        foundry_caller(agent_factory=factory_raising(ValueError("other")))("i", "t")


def test_the_survey_call_sets_no_output_cap():
    # Round 7: on a reasoning model the cap also counted hidden reasoning and cut hard batches short.
    from bom_processor.package_survey_run import foundry_caller
    seen = {}

    class Reply:
        text = '{"version": "package-survey-reply.v2", "files": [], "findings": []}'

    class Factory:
        def __init__(self, **_):
            pass

        async def run(self, _text, options):
            seen.update(options)
            return Reply()

    foundry_caller(agent_factory=Factory)("instructions", "text")
    assert "max_tokens" not in seen and seen["response_format"] == {"type": "json_object"}


# --- one BOM holding several requested assemblies (QuickQuote A045, Nick 2026-10-13) ----------

COMBINED = xlsx({"BOM": [["Combined BOM"], ["Parent Part", "Part Number", "Description", "Qty"],
                         ["ENE010-0810-U2-TK", "ENE010-0810-U2-TK", "PCB ASSY", 1],
                         ["ENE010-0810-U2-TK", "R-100", "Resistor", 2],
                         ["SNC006-002022-01-TK", "SNC006-002022-01-TK", "PCB ASSY", 1],
                         ["SNC006-002022-01-TK", "C-200", "Capacitor", 3]]})


def test_a_bom_naming_two_requested_assemblies_is_kept_for_both_not_set_aside():
    # The survey links a file to one assembly at most; here it links none, as on A045.
    package = archive({"Boards/COMBINED BOM.xlsx": COMBINED})
    _, _, done, _ = run(LinkingAgent(), docs={"doc-combined": COMBINED}, package=package)
    assert done["status"] == "completed"
    document = done["result"]["documents"][0]
    assert (document["decision"], document["reason"]) == ("process", "combined_bom")
    assert document["requested_assembly_ids"] == sorted([ENE_ID, SNC_ID])


def test_a_bom_naming_only_one_requested_assembly_is_still_set_aside_when_unlinked():
    single = xlsx({"BOM": [["One BOM"], ["Parent Part", "Part Number", "Qty"], ["ENE010-0810-U2-TK", "R-100", 2]]})
    package = archive({"Boards/ONE BOM.xlsx": single})
    _, _, done, _ = run(LinkingAgent(), docs={"doc-one": single}, package=package)
    assert done == {"request_id": done["request_id"], "status": "failed", "code": "PACKAGE_NO_REQUESTED_BOM"}
