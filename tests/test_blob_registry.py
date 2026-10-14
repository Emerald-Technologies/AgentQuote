import hashlib
import io
import json

import pandas as pd
import pytest

from bom_processor import ApprovalRecord, BlobRegistryReader, FixtureBinding, RegistryError, VariantSignature
from bom_processor.core import CANONICAL_COLUMNS

PARSER_KEY = "Customer1"
CUSTOMER = "customer1"
CODE = b"approved parser bytes"


def workbook(columns=None):
    stream = io.BytesIO()
    pd.DataFrame([[1, "P1", "Widget"]], columns=columns or ["line_number", "item_number", "item_name"]).to_excel(stream, index=False)
    return stream.getvalue()


def fixture_blobs(signature=None, code=CODE):
    signature = signature or VariantSignature(frozenset({".xlsx"}), required_sheet_names=frozenset({"Sheet1"}), required_headers={"Sheet1": frozenset({"line_number", "item_number", "item_name"})})
    fixture = FixtureBinding("fixture1", "1" * 64, "2" * 64, "3" * 64)
    code_hash = hashlib.sha256(code).hexdigest()
    candidate_hash = "4" * 64
    fixture_hash = hashlib.sha256(json.dumps([fixture.to_dict()], sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    approval = ApprovalRecord("approval1", "candidate1", PARSER_KEY, CUSTOMER, "format1", "1", candidate_hash, code_hash, signature.signature_hash, signature.rule_hash, fixture_hash, "admin1", "2026-09-20T00:00:00Z")
    base = f"approved/{PARSER_KEY}/format1/1"
    master = {"schema": "master-registry.v1", "parser_key": PARSER_KEY, "generation": 1, "variants": [{
        "variant_id": "format1", "version": "1", "code_path": f"{base}/parse.py",
        "code_sha256": code_hash, "signature_hash": signature.signature_hash,
        "rule_hash": signature.rule_hash, "approval_id": approval.approval_id,
        "signature_path": f"{base}/signature.json", "approval_path": f"{base}/approval.json",
    }]}
    return {
        f"registry/{PARSER_KEY}/master.json": json.dumps(master).encode(),
        f"{base}/signature.json": json.dumps(signature.to_dict()).encode(),
        f"{base}/approval.json": json.dumps(approval.to_dict()).encode(),
        f"{base}/parse.py": code,
    }


def reader(blobs):
    def read(path, max_bytes):
        if path not in blobs:
            raise FileNotFoundError(path)
        value = blobs[path]
        if len(value) > max_bytes:
            raise ValueError("too large")
        return value
    return BlobRegistryReader(read)


def test_valid_registry_selects_exact_approved_code():
    selected = reader(fixture_blobs()).select(PARSER_KEY, CUSTOMER, workbook(), ".xlsx")
    assert selected.variant.code_path == f"approved/{PARSER_KEY}/format1/1/parse.py"
    assert selected.code == CODE
    assert selected.generation == 1


@pytest.mark.parametrize("target", ["code", "signature", "approval"])
def test_registry_tampering_fails_closed(target):
    blobs = fixture_blobs()
    path = next(path for path in blobs if (target == "code" and path.endswith("parse.py")) or (target == "signature" and path.endswith("signature.json")) or (target == "approval" and path.endswith("approval.json")))
    blobs[path] += b"tampered"
    with pytest.raises(RegistryError, match="REGISTRY_CORRUPT"):
        reader(blobs).select(PARSER_KEY, CUSTOMER, workbook(), ".xlsx")


def test_no_match_and_ambiguous_are_structured():
    blobs = fixture_blobs()
    with pytest.raises(RegistryError) as missing:
        reader(blobs).select(PARSER_KEY, CUSTOMER, workbook(["other", "columns", "only"]), ".xlsx")
    assert missing.value.code == "PARSER_FORMAT_NOT_RECOGNIZED"
    master_path = f"registry/{PARSER_KEY}/master.json"
    master = json.loads(blobs[master_path])
    duplicate = dict(master["variants"][0]); duplicate["variant_id"] = "format2"; duplicate["version"] = "1"
    old = f"approved/{PARSER_KEY}/format1/1"; new = f"approved/{PARSER_KEY}/format2/1"
    duplicate.update(code_path=f"{new}/parse.py", signature_path=f"{new}/signature.json", approval_path=f"{new}/approval.json", approval_id="approval2")
    approval_data = json.loads(blobs[f"{old}/approval.json"]); approval_data.update(approval_id="approval2", variant_id="format2"); approval_data.pop("approval_hash")
    approval = ApprovalRecord(**approval_data)
    blobs[f"{new}/parse.py"] = blobs[f"{old}/parse.py"]
    blobs[f"{new}/signature.json"] = blobs[f"{old}/signature.json"]
    blobs[f"{new}/approval.json"] = json.dumps(approval.to_dict()).encode()
    master["variants"].append(duplicate); blobs[master_path] = json.dumps(master).encode()
    with pytest.raises(RegistryError) as ambiguous:
        reader(blobs).select(PARSER_KEY, CUSTOMER, workbook(), ".xlsx")
    assert ambiguous.value.code == "PARSER_FORMAT_AMBIGUOUS"


def test_absent_registry_is_distinct_for_legacy_fallback():
    with pytest.raises(RegistryError) as absent:
        reader({}).select(PARSER_KEY, CUSTOMER, workbook(), ".xlsx")
    assert absent.value.code == "REGISTRY_ABSENT"
