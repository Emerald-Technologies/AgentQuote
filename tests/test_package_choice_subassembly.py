"""Sub-assembly files in the package choice (sub-assembly plan unit 6b, Nick 2026-10-13). Offline, stub agent.

A BOM of an assembly that was not requested, named by a part-number cell of a requested assembly's BOM,
is processed as a sub-assembly file instead of being set aside, and is never a candidate for the parent's
main BOM. Same package as test_package_requested_assemblies, plus a cable BOM. Synthetic data only.
Run: pytest tests/test_package_choice_subassembly.py
"""
import json

from bom_processor.package_resolution import requested_identity
from bom_processor.semantic import AssemblyIdentity
from test_bom_package_choice import HEADER, lines
from test_package_requested_assemblies import (ENE_MOD, ENE_PLAIN, FILES, MPI, SIBLING, LinkingAgent, codes,
                                               decisions, path_item, run)
from test_package_survey import archive, xlsx

CABLE = xlsx({"BOM": [["Assembly", "CBL009-0001-00"], HEADER] + lines(2, start=70)})
NAMING_SNC = xlsx({"BOM": [["Assembly", "006-002022-01"], HEADER] + lines(4, start=50)
                   + [[99, "CBL009-0001-00", None, None, 2]]})
NAMING_ENE = xlsx({"BOM": [["Energous Yondu V10 013-0810-00p1", "v10p1"], HEADER] + lines(6)
                   + [[99, "CBL009-0001-00", None, None, 1]]})


class OwnAssemblyAgent(LinkingAgent):
    """As LinkingAgent, but a file whose name starts with CBL009 declares that assembly (cited from its name)."""

    def __call__(self, instructions, text):
        reply = super().__call__(instructions, text)
        body, _ = json.JSONDecoder().raw_decode(text)
        paths = {entry["id"]: entry["path"] for entry in body.get("files", [])}
        if "classify" in body:
            for item in reply["files"]:
                if paths[item["file_id"]].rsplit("/", 1)[-1].startswith("CBL009"):
                    item["assembly"] = {"name": [path_item("CBL009-0001-00")], "revision": []}
        else:
            self.choice_paths = sorted(paths.values())
        return reply


def test_a_cable_bom_in_its_own_folder_is_processed_when_a_requested_bom_names_it():
    files = {**FILES, "SNC006-002022-01-TK/SNC BOM.xlsx": NAMING_SNC, "CBL009-0001-00/CBL BOM.xlsx": CABLE}
    docs = {"doc-ene-mod": ENE_MOD, "doc-ene": ENE_PLAIN, "doc-snc": NAMING_SNC, "doc-sib": SIBLING,
            "doc-mpi": MPI, "doc-cbl": CABLE}
    _, _, state, _ = run(docs=docs, package=archive(files))
    assert state["status"] == "completed"
    found = decisions(state)
    assert found["doc-cbl"] == ("process", "subassembly_of")
    assert found["doc-sib"] == ("excluded", "not_mapped")  # named by no BOM: still set aside
    cable = next(item for item in state["result"]["documents"] if item["document_id"] == "doc-cbl")
    snc_files = next(item for item in state["result"]["documents"] if item["document_id"] == "doc-snc")["survey_file_ids"]
    assert cable["named_by_file_ids"] == snc_files
    assert codes(state)["SURVEY_BOM_NOT_MAPPED"]["document_ids"] == ["doc-sib"]


def test_a_cable_bom_in_the_parent_folder_is_kept_out_of_the_main_bom_choice():
    files = {**FILES, "ENE010-0810-U2 v10p1/ENE MOD BOM.xlsx": NAMING_ENE,
             "ENE010-0810-U2 v10p1/CBL009-0001-00 BOM.xlsx": CABLE}
    docs = {"doc-ene-mod": NAMING_ENE, "doc-ene": ENE_PLAIN, "doc-snc": FILES["SNC006-002022-01-TK/SNC BOM.xlsx"],
            "doc-sib": SIBLING, "doc-mpi": MPI, "doc-cbl": CABLE}
    agent = OwnAssemblyAgent()
    _, _, state, _ = run(agent=agent, docs=docs, package=archive(files))
    found = decisions(state)
    assert found["doc-cbl"] == ("process", "subassembly_of")
    assert found["doc-ene-mod"] == ("process", "primary") and found["doc-ene"][0] == "excluded"
    assert not any("CBL009" in path for path in agent.choice_paths)


def test_a_sub_assembly_file_keeps_its_own_identity_at_review():
    result = {"requested_assemblies": [{"assembly_id": "a", "part_number": "ENE010-0810-U2-TK", "revision": ""}],
              "documents": [{"document_id": "doc-cbl", "requested_assembly_ids": ["a"], "reason": "subassembly_of"},
                            {"document_id": "doc-x", "requested_assembly_ids": ["a"], "reason": "only_bom"}]}
    parsed = AssemblyIdentity("CBL009-0001-00", "")
    assert requested_identity(result, ["doc-cbl"], [{"item_number": "P-70"}], parsed) is None
    # An ordinary BOM linked to the requested assembly still takes its identity, as before.
    assert requested_identity(result, ["doc-x"], [{"item_number": "P-70"}], parsed).name == "ENE010-0810-U2-TK"
