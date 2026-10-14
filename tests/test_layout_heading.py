"""Layout by column-heading row (option C, Nick 2026-10-09). Synthetic BOMs only."""
import hashlib
import io
import json
from datetime import datetime, timezone

import openpyxl
import pandas as pd
import pytest

from bom_processor import BlobRegistryReader, InMemoryStorage, StartResult, VariantSignature
from bom_processor.admin_promotion import AdminDecisionService
from bom_processor.candidate_authoring import CandidateAuthoringService
from bom_processor.candidate_validation import CandidateValidationService
from bom_processor.candidates import persist_candidate
from bom_processor.core import CANONICAL_COLUMNS, OUTPUT_CONTENT_TYPE
from bom_processor.layout_heading import find_heading, heading_agreement, validated_heading
from bom_processor.master_registry import RegistryError, _hash, profile_bytes
from row_map_fixtures import derive_row_map

HEADER = ["Item", "Part Number", "Description", "Manufacturer", "MPN", "Qty"]


def bom(title_rows, parts, header=HEADER):
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "BOM"
    for row in title_rows:
        sheet.append(row)
    sheet.append(header)
    for index, part in enumerate(parts, 1):
        sheet.append([index, part, "Resistor 10k", "Yageo", f"RC-{part}", 2])
    stream = io.BytesIO()
    book.save(stream)
    return stream.getvalue()


def five_assemblies():
    return [bom([[f"Assembly 100-00{n} Rev {chr(64 + n)}"]], [f"P{n}{i}" for i in range(1, 12)])
            for n in range(1, 6)]


def test_five_assembly_boms_of_one_format_share_one_layout():
    # Nick's case: heading row on row 2, BOM rows below, a different assembly title on row 1.
    profiles = [profile_bytes(data, ".xlsx") for data in five_assemblies()]
    assert len({profile.profile_hash for profile in profiles}) == 1
    assert profiles[0].layout["sheets"]["BOM"] == {
        "row": 2, "headings": ["item", "part_number", "description", "manufacturer", "mpn", "qty"]}


def test_heading_row_position_and_title_block_do_not_matter():
    short = bom([["Assembly 100-001"]], ["A1", "A2"])
    long = bom([["Customer", "ACME"], ["Assembly", "100-002"], ["Rev", "B"], []], ["B1", "B2", "B3"])
    first, second = profile_bytes(short, ".xlsx"), profile_bytes(long, ".xlsx")
    assert first.layout["sheets"]["BOM"]["row"] == 2
    assert second.layout["sheets"]["BOM"]["row"] == 5
    assert first.profile_hash == second.profile_hash


def test_different_headings_are_different_layouts():
    other = bom([["Assembly 100-001"]], ["A1"], header=HEADER[:-1] + ["Quantity"])
    assert profile_bytes(other, ".xlsx").profile_hash != profile_bytes(five_assemblies()[0], ".xlsx").profile_hash


def test_title_block_with_numbers_or_few_cells_is_never_the_heading():
    rows = [(1, ("assembly", "100_001", "rev", "a")), (2, ("customer", "acme")),
            (3, ("item", "part_number", "qty")), (4, ("1", "p1", "2")), (5, ("2", "p2", "4"))]
    assert find_heading(rows) == {"row": 3, "headings": ["item", "part_number", "qty"]}
    # A text row with nothing under it is not a heading.
    assert find_heading([(1, ("notes", "see", "drawing")), (2, ("", "", "x"))]) is None
    # Beyond the scanned rows nothing is found.
    assert find_heading([(31, ("item", "part", "qty")), (32, ("1", "p", "2"))]) is None


def test_no_heading_keeps_the_previous_fingerprint():
    data = b"Part,Qty\nP1,2\n"
    profile = profile_bytes(data, ".csv")
    assert profile.layout["sheets"]["Sheet1"] is None
    legacy = profile.to_dict(False)
    for key in ("size_bytes", "sha256", "layout"):
        legacy.pop(key)
    assert profile.profile_hash == _hash(legacy)


def test_heading_rule_reuses_a_parser_for_another_assembly_and_first_row_rule_does_not():
    first, *others = [profile_bytes(data, ".xlsx") for data in five_assemblies()]
    heading = VariantSignature.from_profile(first.to_dict(), heading=True)
    old = VariantSignature.from_profile(first.to_dict())
    assert all(heading.matches(profile) for profile in others)
    assert not any(old.matches(profile) for profile in others)
    assert not heading.matches(profile_bytes(bom([["X"]], ["A1"], header=HEADER[:-1] + ["Quantity"]), ".xlsx"))


def test_heading_rule_round_trips_and_old_rules_keep_their_hashes():
    profile = profile_bytes(five_assemblies()[0], ".xlsx")
    heading = VariantSignature.from_profile(profile.to_dict(), heading=True)
    stored = heading.to_dict()
    assert VariantSignature.from_dict(stored) == heading
    with pytest.raises(RegistryError, match="REGISTRY_CORRUPT"):
        VariantSignature.from_dict({**stored, "required_heading": {"BOM": ["item"]}})
    old = VariantSignature.from_profile(profile.to_dict())
    assert "required_heading" not in old.to_dict()
    assert old.rule_hash == _hash({key: value for key, value in old.to_dict(False).items()})
    no_heading = profile_bytes(b"Part,Qty\nP1,2\n", ".csv").to_dict()
    with pytest.raises(RegistryError, match="INVALID_SIGNATURE"):
        VariantSignature.from_profile(no_heading, heading=True)


def row_map(lines, skipped, sheet="BOM"):
    return json.dumps({"version": "bom-row-map.v1", "ignored_columns": [],
                       "lines": [{"output_row": i + 2, "sheet": sheet, "rows": [row], "fields": {}}
                                 for i, row in enumerate(lines)],
                       "skipped": [{"sheet": sheet, "rows": [[a, b]], "reason": reason, "rationale": "r"}
                                   for a, b, reason in skipped]}).encode()


def test_agreement_needs_the_heading_row_skipped_as_header_and_lines_below_it():
    data = five_assemblies()[0]
    good = row_map(range(3, 14), [(1, 1, "title"), (2, 2, "header")])
    assert heading_agreement(data, ".xlsx", good)["agreed"] is True
    wrong_row = row_map(range(3, 14), [(1, 1, "header"), (2, 2, "title")])
    assert heading_agreement(data, ".xlsx", wrong_row)["reason"] == "HEADING_NOT_SKIPPED_AS_HEADER"
    above = row_map([1] + list(range(3, 14)), [(2, 2, "header")])
    assert heading_agreement(data, ".xlsx", above)["reason"] == "LINE_ABOVE_HEADING"
    assert heading_agreement(data, ".xlsx", b"not json")["agreed"] is False
    csv = b"Assembly A-100\nmystery,name,qty\nX,Widget,2\n"
    assert heading_agreement(csv, ".csv", row_map([3], [(1, 1, "title"), (2, 2, "header")], "CSV"))["agreed"]


def test_validated_heading_requires_every_validated_file_to_agree():
    agreed = {"valid": True, "layout_heading": {"version": "bom-layout.v1", "agreed": True}}
    refused = {"valid": True, "layout_heading": {"version": "bom-layout.v1", "agreed": False}}
    assert validated_heading({"semantic": {"row_accounting": agreed}})
    assert not validated_heading({"semantic": {"row_accounting": refused}})
    assert not validated_heading({"semantic": {"valid": True}})
    group = {"fixtures": [{"fixture_sha256": "a", "semantic": {"row_accounting": agreed}},
                          {"fixture_sha256": "b", "semantic": {"row_accounting": refused}}]}
    assert not validated_heading(group)
    group["fixtures"][1]["semantic"]["row_accounting"] = agreed
    assert validated_heading(group)


# End to end: a parser approved for one assembly's BOM is selected for the next assembly's BOM.
SOURCE_PATH = "customer/rfq-id/package-1/original-boms/doc.csv"
# Maker pairs: a BOM without any manufacturer/MPN pair stops for a person (round 8).
SOURCE = b"Assembly A-100\nmystery,name,qty,maker,maker part\nX,Widget,2,Acme,A-100\n"
NEXT = b"Assembly B-200\nmystery,name,qty,maker,maker part\nY,Gadget,5,Acme,G-5\nZ,Sprocket,1,Acme,S-1\n"
CODE = b'''import argparse\nparser = argparse.ArgumentParser()\nparser.add_argument("--input", required=True)\nparser.add_argument("--output", required=True)\nparser.add_argument("--row-map", required=True)\nargs = parser.parse_args()\n'''
NOW = datetime(2026, 10, 9, 3, 0, tzinfo=timezone.utc)


class Model:
    def generate(self, prompt, source_bytes):
        return CODE.decode()


class Worker:
    """Writes the canonical output and an honest row map: row 1 title, row 2 header, row 3 a line."""

    def __init__(self, storage, header_row=2):
        self.storage, self.header_row = storage, header_row

    def start(self, item):
        row = {column: None for column in CANONICAL_COLUMNS}
        row.update(line_number=1, item_number="X", item_name="Widget", item_category="Part", quantity=2.0,
                   manufacturer_1="Acme", manufacturer_item_number_1="A-100")
        stream = io.BytesIO()
        pd.DataFrame([row], columns=CANONICAL_COLUMNS).to_excel(stream, index=False)
        output = stream.getvalue()
        self.storage.candidates[item.output_blob] = output
        mapped = json.loads(derive_row_map(SOURCE, ".csv", output))
        mapped["skipped"] = [{"sheet": "CSV", "rows": [[r, r]], "reason": "header" if r == self.header_row else "title",
                              "rationale": "Synthetic fixture row."} for r in (1, 2)]
        mapped = json.dumps(mapped).encode()
        self.storage.candidates[item.row_map_blob] = mapped
        result = {"contract_version": "remote-worker.v3", "job": item.job_id, "status": "completed",
                  "input": {"verified": True, "sha256": item.input_sha256, "size": item.input_size},
                  "parser": {"verified": True, "sha256": item.parser_sha256, "size": item.parser_size},
                  "output": {"sha256": hashlib.sha256(output).hexdigest(), "size": len(output),
                             "content_type": OUTPUT_CONTENT_TYPE},
                  "row_map": {"sha256": hashlib.sha256(mapped).hexdigest(), "size": len(mapped),
                              "content_type": "application/json"}}
        self.storage.candidates[item.result_blob] = json.dumps(result).encode()
        return StartResult(item.job_id, "worker-execution", NOW)


def approve(header_row):
    storage = InMemoryStorage(sources={SOURCE_PATH: SOURCE})
    request = persist_candidate(storage, "Customer1", "customer-key", profile_bytes(SOURCE, ".csv"), ({
        "document_id": "doc1", "blob_path": SOURCE_PATH, "size_bytes": len(SOURCE),
        "sha256": hashlib.sha256(SOURCE).hexdigest()},), clock=lambda: NOW)
    candidate = CandidateAuthoringService(request.request_id, storage, Model()).author()
    validation = CandidateValidationService(request.request_id, candidate.candidate_id, storage,
                                            Worker(storage, header_row), sleep=lambda _: None).validate()
    AdminDecisionService(storage, clock=lambda: NOW).decide(
        request_id=request.request_id, candidate_id=candidate.candidate_id, validation_id=validation.validation_id,
        approver={"object_id": "admin-object-1"}, reason="Reviewed synthetic fixture", ticket="CHG-1",
        decision="approve", variant_id="mystery-csv", version="1")
    return storage


def test_parser_approved_for_one_assembly_is_selected_for_the_next_assemblys_bom():
    storage = approve(header_row=2)
    signature = json.loads(storage.registry["approved/Customer1/mystery-csv/1/signature.json"])
    assert signature["required_heading"] == {"Sheet1": ["mystery", "name", "qty", "maker", "maker_part"]}
    selected = BlobRegistryReader(storage.download_registry).select("Customer1", "customer-key", NEXT, ".csv")
    assert selected.variant.variant_id == "mystery-csv"


def test_parser_that_read_another_row_as_header_keeps_the_first_row_rule():
    storage = approve(header_row=1)
    signature = json.loads(storage.registry["approved/Customer1/mystery-csv/1/signature.json"])
    assert "required_heading" not in signature
    with pytest.raises(RegistryError, match="PARSER_FORMAT_NOT_RECOGNIZED"):
        BlobRegistryReader(storage.download_registry).select("Customer1", "customer-key", NEXT, ".csv")
