"""Multi-assembly labor plan step 1 (Nick 2026-10-12): survey clues in the v6 evidence producer, and a
not-text-verified link for a file without text (decision 6). Synthetic packages only.
Run: python -m pytest tests/test_package_evidence_survey_clues.py
"""
import pytest

from bom_processor.core import InMemoryStorage
from bom_processor.package_evidence_compact import MAX_CALL_INPUT_BYTES, _Plan, check_reply
from bom_processor.package_evidence_contracts import LINK_NOT_TEXT_VERIFIED, digest
from bom_processor.package_evidence_proposal import prepare
from bom_processor.package_evidence_survey_clues import load_survey_files, match_clues
from compact_evidence_fixtures import V6, ScriptedModel, package, produce, source_proof, uuid

PDF = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\nbinary assembly drawing bytes\n"


def pdf_package():
    return package([("ASM-100200", "PCB-555001"), ("ASM-100300", None)], lines=5,
                   extra_files=[("drawing-rev-b.pdf", PDF)])


def survey(assembly_index=1, link="unconfirmed", data=PDF, name="drawing-rev-b.pdf"):
    return [{"path": name, "sha256": digest(data), "kind": "pdf",
             "requested_assembly": {"assembly_id": uuid(1, assembly_index), "link": link}}]


def plan_for(args, survey_files=None, limit=MAX_CALL_INPUT_BYTES):
    context, roots, books = args
    prepared = prepare(context, roots, books, producer="p", model_name="m", policy_version=V6,
                       prompt_sha256="d" * 64, run_id="r", include_classification=True,
                       include_manufacturing=True, retained_bom_sources=source_proof(roots))
    return _Plan(prepared, roots, limit, survey_files)


def pdf_id(plan):
    return next(i for i, path in plan.paths.items() if path.endswith(".pdf"))


class PdfLinkingModel(ScriptedModel):
    """Also links a file without text to the assembly its survey clue names, as decision 6 allows."""

    def __init__(self, target=None, **kwargs):
        super().__init__(**kwargs)
        self.target = target

    def propose(self, packet):
        reply = super().propose(packet)
        for view, entry in zip(packet["files"], reply["files"]):
            clue = view.get("survey")
            if not view.get("quotable") and view["path"].endswith(".pdf") and clue and clue["assembly"]:
                entry.update(state="included", rationale="The survey and the file's folder and name point here.",
                             links=[{"lines": [], "assemblies": [self.target or clue["assembly"]],
                                     "role": "assembly_drawing", "sharing": "not_shared",
                                     "rationale": "Survey clue, folder and name agree.", "cite": []}])
        return reply


def test_clues_match_on_path_and_file_hash_only():
    inventory = [{"evidenceId": "E1", "documentId": "D1", "sha256": "root", "archiveChain": [
        {"memberPath": "a.zip", "sha256": "z" * 64, "sizeBytes": 1}, {"memberPath": "x.pdf", "sha256": "f" * 64, "sizeBytes": 1}]}]
    names = {"D1": "pkg.zip"}
    record = {"path": "pkg.zip/a.zip/x.pdf", "sha256": "f" * 64, "kind": "pdf",
              "requested_assembly": {"assembly_id": "asm-1", "link": "confirmed"}}
    assert match_clues(inventory, names, [record], {"asm-1"}) == {"E1": {"assembly": "asm-1", "link": "confirmed", "kind": "pdf"}}
    assert match_clues(inventory, names, [{**record, "sha256": "0" * 64}], {"asm-1"}) == {}
    assert match_clues(inventory, names, [record, record], {"asm-1"}) == {}          # a path seen twice
    assert match_clues(inventory, names, [record], {"other"})["E1"] == {"assembly": None, "link": "none", "kind": "pdf"}
    assert match_clues(inventory, names, None, {"asm-1"}) == {}


def test_a_missing_survey_never_blocks_the_producer():
    assert load_survey_files(InMemoryStorage(), "a" * 64) is None


def test_the_file_view_carries_the_clue_also_without_content():
    plan = plan_for(pdf_package(), survey())
    assert plan.views[pdf_id(plan)]["survey"] == {"assembly": "A1", "link": "unconfirmed", "kind": "pdf"}
    plain = plan_for(pdf_package())
    assert "survey" not in plain.views[pdf_id(plain)]


def test_a_file_named_for_no_assembly_is_placed_by_its_clue():
    plan = plan_for(pdf_package(), survey(assembly_index=2))
    identity = pdf_id(plan)
    assert identity in plan.placed[uuid(1, 2)] and identity not in plan.shared_files
    plain = plan_for(pdf_package())
    assert pdf_id(plain) in plain.shared_files


def test_a_file_without_text_is_linked_from_its_clue_and_marked_for_a_person():
    result = produce(pdf_package(), model=PdfLinkingModel(), survey_files=survey())
    proposal, assessment = result["proposal"], result["manufacturingAssessment"]
    association = next(a for a in proposal["associations"] if a["citations"])
    assert association["assemblyUuids"] == [uuid(1, 1)]
    assert association["citations"][0]["locator"] == "package survey"
    assert "ASM-100200 (unconfirmed)" in association["citations"][0]["observation"]
    relation = next(r for r in assessment["relationships"] if r["associationId"] == association["associationId"])
    assert relation["conflicts"] == [LINK_NOT_TEXT_VERIFIED] and relation["citations"] == []
    # Text-verified links are unchanged.
    others = [r for r in assessment["relationships"] if r["associationId"] != association["associationId"]]
    assert others and all(r["conflicts"] == [] and r["citations"] for r in others)


@pytest.mark.parametrize("survey_files,target", [(None, None), (survey(), "A2"), (survey(link="none"), None)])
def test_a_file_without_text_is_left_open_without_a_matching_clue(survey_files, target):
    plan = plan_for(pdf_package(), survey_files)
    call = plan.calls()[0]
    reply = ScriptedModel().propose(plan.packet(call))
    pdf_short = plan.file_short[pdf_id(plan)]
    entry = next(f for f in reply["files"] if f["file"] == pdf_short)
    entry.update(state="included", links=[{"lines": [], "assemblies": [target or "A1"], "role": "assembly_drawing",
                                           "sharing": "not_shared", "rationale": "Guess.", "cite": []}])
    # Nick 2026-10-12: the link is dropped and the file left for a person; the rest of the answer stands.
    answer = check_reply(plan, call, reply)
    assert answer["files"][pdf_id(plan)]["state"] == "unresolved" and answer["files"][pdf_id(plan)]["links"] == []
    assert any(a["state"] == "included" for a in answer["files"].values())


def test_an_unresolved_file_carries_its_survey_reading_for_the_review():
    class LeavesPdfOpen(ScriptedModel):
        def propose(self, packet):
            reply = super().propose(packet)
            for view, entry in zip(packet["files"], reply["files"]):
                if view["path"].endswith(".pdf"):
                    entry.update(state="unresolved", links=[], rationale="Cannot tell from what is shown.")
            return reply
    result = produce(pdf_package(), model=LeavesPdfOpen(), survey_files=survey(assembly_index=2, link="confirmed"))
    open_files = [d for d in result["proposal"]["dispositions"] if d["state"] == "unresolved"]
    assert len(open_files) == 1
    assert open_files[0]["citations"] == [{"evidenceId": open_files[0]["evidenceId"], "locator": "package survey",
                                           "observation": "The package survey linked this file to ASM-100300 (confirmed)."}]
