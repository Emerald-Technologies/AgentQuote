import hashlib
import io

import pandas as pd

from bom_processor.arena_policy import is_arena_source, validate, verify_evidence
from bom_processor.core import CANONICAL_COLUMNS


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def arena(rows: list[list[str]], extra_pairs: int = 0) -> bytes:
    headers = [
        "item_number", "item_name", "revision", "item_category", "unit_of_measure",
        "quantity", "reference_designator", "bom_notes",
    ]
    for index in range(1, 9 + extra_pairs):
        headers.extend((f"manufacturer_{index}", f"manufacturer_item_number_{index}"))
    width = len(headers)
    body = []
    for row in rows:
        cells = row + [""] * (width - len(row))
        body.append("<tr>" + "".join(f"<td>{value}</td>" for value in cells) + "</tr>")
    return ("<html><head></head><body><table><tr>" +
            "".join(f"<th>{value}</th>" for value in headers) + "</tr>" +
            "".join(body) + "</table></body></html>").encode()


def output(rows: list[dict]) -> bytes:
    frame = pd.DataFrame(rows, columns=CANONICAL_COLUMNS)
    stream = io.BytesIO()
    frame.to_excel(stream, index=False, engine="openpyxl")
    return stream.getvalue()


def parsed(item: str, quantity: float, manufacturer="Maker", mpn="MPN-1") -> dict:
    row = {name: None for name in CANONICAL_COLUMNS}
    row.update({
        "item_number": item,
        "item_name": "Component",
        "item_category": "Part",
        "quantity": quantity,
        "manufacturer_1": manufacturer,
        "manufacturer_item_number_1": mpn,
    })
    return row


def validate_fixture(source: bytes, parsed_bytes: bytes):
    return validate(source, parsed_bytes, source_sha256=sha(source),
                    candidate_code_sha256="c" * 64, output_sha256=sha(parsed_bytes))


def test_retains_reference_rows_and_omits_only_existing_parent_case():
    source = arena([
        ["ASM-1", "Assembly", "A", "Printed Circuit Board Assembly", "EA", "", "", ""],
        ["PROC-1", "Programming", "A", "Procedure", "EA", "0", "", "Reference"],
        ["DRAW-1", "Schematic", "A", "Drawing", "EA", "0", "", "Reference"],
        ["P1", "Component", "A", "Resistor", "EA", "2", "R1", "SMT", "Maker", "MPN-1"],
    ])
    documents = [parsed("PROC-1", 0, "", ""), parsed("DRAW-1", 0, "", "")]
    for row in documents:
        row["item_category"] = "Document"
    parsed_bytes = output(documents + [parsed("P1", 2)])
    evidence = validate_fixture(source, parsed_bytes)
    assert evidence["valid"] is True
    assert evidence["included_count"] == 3
    assert evidence["omitted_count"] == 1
    assert [item["item_number"] for item in evidence["omissions"]] == ["ASM-1"]
    assert verify_evidence(evidence) is True
    evidence["omissions"][0]["item_number"] = "CHANGED"
    assert verify_evidence(evidence) is False
    assert [decision["decision"] for decision in evidence["decisions"]] == [
        "omitted_parent_blank_quantity",
        "include",
        "include",
        "include",
    ]


def test_blank_or_zero_component_quantity_blocks():
    for quantity, reason in [("", "blank_quantity"), ("0", "zero_quantity")]:
        source = arena([["P1", "Component", "A", "Resistor", "EA", quantity, "R1", "SMT", "Maker", "MPN-1"]])
        evidence = validate_fixture(source, output([parsed("P1", 1)]))
        assert evidence["valid"] is False
        assert evidence["reason"] == reason


def test_parent_omission_requires_first_row_and_exact_evidence():
    source = arena([
        ["P1", "Component", "A", "Resistor", "EA", "1", "R1", "SMT", "Maker", "MPN-1"],
        ["ASM-1", "Assembly", "A", "Printed Circuit Board Assembly", "EA", "", "", ""],
    ])
    evidence = validate_fixture(source, output([parsed("P1", 1)]))
    assert evidence["valid"] is False
    assert evidence["reason"] == "blank_quantity"


def test_preserves_first_eight_pairs_and_discloses_later_pairs():
    base = ["P1", "Component", "A", "Resistor", "EA", "1", "R1", "SMT"]
    pairs = []
    for index in range(1, 10):
        pairs.extend((f"Maker {index}", f"MPN-{index}"))
    source = arena([base + pairs], extra_pairs=1)
    row = parsed("P1", 1)
    for index in range(1, 9):
        row[f"manufacturer_{index}"] = f"Maker {index}"
        row[f"manufacturer_item_number_{index}"] = f"MPN-{index}"
    evidence = validate_fixture(source, output([row]))
    assert evidence["valid"] is True
    assert any(item.get("decision") == "omitted_by_contract" and item.get("pair") == 9
               for item in evidence["decisions"])


def test_rejects_missing_component_or_changed_manufacturer_pair():
    source = arena([["P1", "Component", "A", "Resistor", "EA", "1", "R1", "SMT", "Maker", "MPN-1"]])
    assert validate_fixture(source, output([]))["reason"] == "identity_coverage"
    assert validate_fixture(source, output([parsed("P1", 1, mpn="CHANGED")]))["reason"] == "manufacturer_pair_mismatch"


def test_detection_requires_arena_header_family():
    source = arena([["P1", "Component", "A", "Resistor", "EA", "1", "R1", "SMT", "Maker", "MPN-1"]])
    assert is_arena_source(source) is True
    assert is_arena_source(b"<html><head></head><body><table><tr><th>part</th></tr></table></body></html>") is False
