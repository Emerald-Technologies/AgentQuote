"""Heading clues, a fresh attempt after a repeated crash, and the parent-line example (QuickQuote A048).

A048's combined legacy .xls BOM has a plain heading row, yet five of six authored parsers stopped with
"Input workbook holds no BOM table" (one heading-search slip kept through four refinements), and the
sixth wrote parent "line" as a position instead of an output_row. Synthetic data only.
"""
import hashlib

from bom_processor import InMemoryStorage
from bom_processor.candidate_authoring import AUTHORING_VERSION, ROW_MAP_RULE, CandidateAuthoringService
from bom_processor.candidate_validation import _ROW_MAP_SHAPES
from bom_processor.candidates import persist_candidate
from bom_processor.core import _json_bytes
from bom_processor.master_registry import profile_bytes
from bom_processor.row_refinement import crash_signature, repeated_crash
from bom_processor.table_heading import heading_clues, heading_rule
from legacy_xls import legacy_xls

HEADINGS = ["ORIGINAL", "Parent Part", "Part Number", "Indented Part Number", "Revision", "Description",
            "Quantity", "UOM", "Level", "Find No", "Release Status"]
BOM = [HEADINGS,
       ["BRD-1", "BRD-1", "BRD-1", "BRD-1", "02", "PCB ASSY, BOARD ONE", 1.0, "EA", 0, "0000", "EVALUATION"],
       ["BRD-1", "BRD-1", "ART-1", "**ART-1", "02", "PCB ARTWORK, BOARD ONE", 1.0, "EA", 1, "0000", "EVALUATION"],
       ["BRD-1", "BRD-1", "CAP-1", "**CAP-1", "01", "CAP CER 33PF", 3.0, "EA", 1, "0002", "PRODUCTION"],
       ["BRD-2", "BRD-2", "BRD-2", "BRD-2", "02", "PCB ASSY, BOARD TWO", 1.0, "EA", 0, "0000", "EVALUATION"],
       ["BRD-2", "BRD-2", "WIRE-2", "**WIRE-2", "01", "SILVER WIRE", 3.0, "EA", 1, "0002", "PRODUCTION"]]
MFG = [["Part Number", "Manufacturer Name", "Manufacturer Part Number"], ["CAP-1", "Acme", "A-33"]]


def test_the_heading_row_and_its_suggested_roles_are_read_from_a_legacy_xls():
    clues = heading_clues(legacy_xls({"BOM": BOM, "MFG": MFG}), ".xls")
    bom = next(clue for clue in clues if clue["sheet"] == "BOM")
    assert bom["heading_row"] == 1
    suggests = {item["heading"]: item.get("suggests") for item in bom["columns"]}
    assert suggests["Parent Part"] == "parent part number" and suggests["Part Number"] == "part number"
    assert suggests["Indented Part Number"] == "indented part number" and suggests["Level"] == "level"
    assert suggests["Description"] == "description" and suggests["Quantity"] == "quantity"
    assert suggests["Find No"] == "find number" and suggests["Revision"] == "revision"
    assert suggests["ORIGINAL"] is None and suggests["Release Status"] is None
    assert [item["column"] for item in bom["columns"]] == list(range(1, len(HEADINGS) + 1))


def test_a_title_row_above_the_table_is_not_taken_for_the_heading():
    rows = [["Bill of materials for BRD-1"], [None], HEADINGS] + BOM[1:]
    clues = heading_clues(legacy_xls({"BOM": rows}), ".xls")
    assert [(clue["sheet"], clue["heading_row"]) for clue in clues] == [("BOM", 3)]


def test_no_clue_without_a_heading_row_and_no_rule_without_clues():
    notes = [["Notes"], ["Build per drawing"], ["Revision history kept elsewhere"]]
    assert heading_clues(legacy_xls({"Notes": notes}), ".xls") == []
    assert heading_clues(b"not a workbook", ".xls") == []
    assert heading_rule([]) is None


def test_the_rule_states_the_clue_as_a_clue_with_1_based_numbers():
    rule = heading_rule(heading_clues(legacy_xls({"BOM": BOM}), ".xls"))
    assert rule.startswith("HEADING CLUES") and '"heading_row":1' in rule
    assert "subtract 1" in rule and "a clue, not a rule" in rule and "Do not write your own heading search" in rule


class Model:
    def __init__(self):
        self.prompts = []

    def generate(self, prompt, source_bytes):
        self.prompts.append(prompt)
        return PRIOR.replace("# prior parser", "# new parser")


PRIOR = ('import argparse\np = argparse.ArgumentParser()\np.add_argument("--input")\np.add_argument("--output")\n'
         'p.add_argument("--row-map")\n# prior parser\n')
CRASH = "parser failed | ValueError: Input workbook holds no BOM table Traceback (most recent call last): line {}"


def authored(source):
    path = "customer/rfq-id/package-1/original-boms/doc.xls"
    storage = InMemoryStorage(sources={path: source})
    request = persist_candidate(storage, "Customer1", "customer-key", profile_bytes(source, ".xls"), ({
        "document_id": "doc1", "blob_path": path, "size_bytes": len(source),
        "sha256": hashlib.sha256(source).hexdigest()},))
    return storage, request


def crashed(storage, request_id, candidate_id, line):
    storage.candidates[f"candidates/{request_id}/{candidate_id}/parse.py"] = PRIOR.encode()
    storage.candidates[f"candidates/{request_id}/{candidate_id}/candidate-manifest.json"] = _json_bytes({
        "candidate_id": candidate_id, "code_sha256": hashlib.sha256(PRIOR.encode()).hexdigest()})
    storage.candidates[f"candidates/events/{request_id}/{candidate_id}/validation-failed-v1.json"] = _json_bytes({
        "event": "candidate.validation_failed", "error_code": "WORKER_PARSER_FAILED", "failure": "RuntimeError",
        "candidate_id": candidate_id, "validation_id": "v1", "repairable": True,
        "failure_message": CRASH.format(line)})


def refined(storage, request_id, parent, child):
    root = f"candidate-automation/{request_id}/author-refinement/{AUTHORING_VERSION}"
    storage.registry[f"{root}/attempt-1.started.json"] = _json_bytes({"refinement_of": parent})
    storage.registry[f"{root}/attempt-1.completed.json"] = _json_bytes({"candidate_id": child})


def test_a_fresh_author_gets_the_heading_clue():
    storage, request = authored(legacy_xls({"BOM": BOM}))
    model = Model()
    CandidateAuthoringService(request.request_id, storage, model).author()
    assert any(rule.startswith("HEADING CLUES") and '"sheet":"BOM"' in rule for rule in model.prompts[0].rules)


def test_a_first_crash_is_patched_from_the_prior_code():
    storage, request = authored(legacy_xls({"BOM": BOM}))
    crashed(storage, request.request_id, "c0", 10)
    model = Model()
    CandidateAuthoringService(request.request_id, storage, model, refine=("c0", "v1")).author()
    prompt = model.prompts[0]
    assert prompt.structural_profile["previous_parser_code"] == PRIOR
    assert any(rule.startswith("PARSER CRASH REFINEMENT") for rule in prompt.rules)


def test_the_same_crash_twice_in_a_row_starts_fresh_without_the_prior_code():
    storage, request = authored(legacy_xls({"BOM": BOM}))
    crashed(storage, request.request_id, "c0", 10)
    crashed(storage, request.request_id, "c1", 42)  # same error, other traceback lines
    refined(storage, request.request_id, "c0", "c1")
    model = Model()
    CandidateAuthoringService(request.request_id, storage, model, refine=("c1", "v1")).author()
    prompt = model.prompts[0]
    assert "previous_parser_code" not in prompt.structural_profile
    fresh = [rule for rule in prompt.rules if rule.startswith("FRESH PARSER ATTEMPT")]
    assert len(fresh) == 1 and "Input workbook holds no BOM table" in fresh[0]
    assert not any(rule.startswith("PARSER CRASH REFINEMENT") for rule in prompt.rules)
    assert any(rule.startswith("HEADING CLUES") for rule in prompt.rules)


def test_a_different_crash_after_a_crash_is_still_patched():
    storage, request = authored(legacy_xls({"BOM": BOM}))
    crashed(storage, request.request_id, "c0", 10)
    crashed(storage, request.request_id, "c1", 42)
    other = "parser failed | KeyError: 'Quantity' Traceback (most recent call last): line 7"
    event_path = f"candidates/events/{request.request_id}/c0/validation-failed-v1.json"
    storage.candidates[event_path] = storage.candidates[event_path].replace(
        CRASH.format(10).encode(), other.encode())
    refined(storage, request.request_id, "c0", "c1")
    paths = lambda prefix: storage.list_registry_paths(prefix, 100)
    assert repeated_crash(storage, request.request_id, "c1", CRASH.format(42), AUTHORING_VERSION, paths) is False
    model = Model()
    CandidateAuthoringService(request.request_id, storage, model, refine=("c1", "v1")).author()
    assert model.prompts[0].structural_profile["previous_parser_code"] == PRIOR


def test_the_crash_signature_ignores_traceback_lines():
    assert crash_signature(CRASH.format(1)) == crash_signature(CRASH.format(99)) == \
        "ValueError: Input workbook holds no BOM table"


def test_the_parent_rule_has_the_worked_example_and_names_the_mistake():
    assert "never its line_number, its position in lines or a source row" in ROW_MAP_RULE
    assert 'output_rows 3-7 have "line": 2 and output_rows 9-13 have "line": 8' in ROW_MAP_RULE
    assert "never 1, its line_number or its position in lines" in _ROW_MAP_SHAPES["ROW_MAP_PARENT_INVALID"]
