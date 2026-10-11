import json
from pathlib import Path

import pytest

from bom_processor import InMemoryStorage
from bom_processor.candidate_authoring import (AUTHORING_VERSION, CANONICAL_OUTPUT_RULES,
                                                AuthoringError, CandidateAuthoringService)
from bom_processor.candidates import persist_candidate
from bom_processor.master_registry import profile_bytes

SOURCE_PATH = "customer/rfq-id/package-1/original-boms/doc.xlsx"
SOURCE = b"fake workbook structural bytes"


def request_storage():
    storage = InMemoryStorage(sources={SOURCE_PATH: SOURCE})
    profile = profile_bytes(b"mystery,qty\nX,2\n", ".csv")
    request = persist_candidate(storage, "Customer1", "customer-key", profile, ({
        "document_id": "doc1", "blob_path": SOURCE_PATH,
        "size_bytes": len(SOURCE), "sha256": __import__("hashlib").sha256(SOURCE).hexdigest(),
    },))
    return storage, request


GOOD = '''import argparse
import csv
import json
import sys
parser = argparse.ArgumentParser()
parser.add_argument("--input", required=True)
parser.add_argument("--output", required=True)
parser.add_argument("--row-map", required=True)
args = parser.parse_args()
with open(args.input, encoding="utf-8") as source:
    rows = list(csv.reader(source))
with open(args.output, "w", encoding="utf-8") as target:
    json.dump({"rows": rows}, target)
if __name__ == "__main__":
    pass
'''


class Model:
    def __init__(self, code=GOOD):
        self.code = code
        self.calls = []

    def generate(self, prompt, source_bytes):
        self.calls.append((prompt, source_bytes))
        return self.code


class CorrectingModel(Model):
    def __init__(self, first, second):
        super().__init__(first)
        self.second = second

    def generate(self, prompt, source_bytes, prior_code=None, failed_rule_id=None):
        self.calls.append((prompt, source_bytes, prior_code, failed_rule_id))
        return self.second if prior_code is not None else self.code


def test_authoring_stores_only_quarantined_immutable_artifacts_without_execution(tmp_path):
    storage, request = request_storage()
    marker = tmp_path / "executed"
    code = GOOD + f"\n# This text must never run\n# {marker}\n"
    model = Model(code)
    result = CandidateAuthoringService(request.request_id, storage, model).author()
    assert result.status == "generated_unvalidated"
    assert not marker.exists()
    prefix = f"candidates/{request.request_id}/{result.candidate_id}"
    assert storage.candidates[f"{prefix}/parse.py"] == code.encode()
    manifest = json.loads(storage.candidates[f"{prefix}/candidate-manifest.json"])
    assert manifest["status"] == "generated_unvalidated"
    assert manifest["version"] == AUTHORING_VERSION
    assert manifest["request_id"] == request.request_id
    assert all(not path.startswith("approved/") and not path.startswith("registry/") for path in storage.candidates)
    prompt, source = model.calls[0]
    assert source == SOURCE
    assert SOURCE.decode(errors="ignore") not in repr(prompt)
    assert prompt.rules == CANONICAL_OUTPUT_RULES
    # The enforced broad-exception rule is stated to the author, not only checked afterwards.
    assert any("except Exception" in rule and "raise statement" in rule for rule in prompt.rules)
    # The banned calls (rule html_xls_banned_workbook_api) are stated too (round 2, item 4).
    assert any("os.system" in rule and "pandas.read_html" in rule for rule in prompt.rules)
    assert any("real XLSX" in rule for rule in prompt.rules)
    assert any("actual document ID in item_number" in rule for rule in prompt.rules)
    assert any("not manufacturer/MPN pairs" in rule for rule in prompt.rules)
    assert any("primary and alternate" in rule for rule in prompt.rules)


def test_html_rules_require_source_taxonomy_mapping_without_fabrication():
    html_source = (b"<html><body><table><tr><th>Category</th><th>Part</th></tr>"
                   b"<tr><td>Resistor</td><td>R-10</td></tr></table></body></html>")
    path = "customer/rfq-id/package-1/original-boms/doc.xls"
    storage = InMemoryStorage(sources={path: html_source})
    profile = profile_bytes(html_source, ".xls")
    request = persist_candidate(storage, "Customer1", "customer-key", profile, ({
        "document_id": "doc1", "blob_path": path, "size_bytes": len(html_source),
        "sha256": __import__("hashlib").sha256(html_source).hexdigest(),
    },))
    model = Model()
    CandidateAuthoringService(request.request_id, storage, model).author()
    rules = model.calls[0][0].rules
    assert any("Resistor" in rule and "item_category='Part'" in rule for rule in rules)
    assert any("actual source category" in rule and "never fabricate" in rule for rule in rules)
    assert any("pandas.read_excel" in rule and "HTMLParser" in rule for rule in rules)


def test_changed_code_creates_new_candidate_without_replacing_prior_artifacts():
    storage, request = request_storage()
    first = CandidateAuthoringService(request.request_id, storage, Model()).author()
    first_paths = dict(storage.candidates)
    second_code = GOOD + "\n# corrected parser revision\n"
    second = CandidateAuthoringService(request.request_id, storage, Model(second_code)).author()
    assert second.candidate_id != first.candidate_id
    assert all(storage.candidates[path] == data for path, data in first_paths.items())
    assert storage.candidates[f"candidates/{request.request_id}/{second.candidate_id}/parse.py"] == second_code.encode()


def test_identical_model_output_is_idempotent():
    storage, request = request_storage()
    service = CandidateAuthoringService(request.request_id, storage, Model())
    first = service.author()
    second = service.author()
    assert second == first
    assert len(storage.candidates) == 3


@pytest.mark.parametrize("code", [
    'import socket\n# --input --output\n',
    'import subprocess\n# --input --output\n',
    'import os\nos.system("bad")\n# --input --output\n',
    'eval("1")\n# --input --output\n',
    'import argparse\n# missing required markers\n',
])
def test_malicious_or_invalid_model_output_is_rejected(code):
    storage, request = request_storage()
    with pytest.raises(AuthoringError) as failure:
        CandidateAuthoringService(request.request_id, storage, Model(code)).author()
    assert failure.value.code in {"CANDIDATE_CODE_POLICY", "CANDIDATE_CODE_INVALID"}
    assert storage.candidates == {}


def test_source_integrity_failure_prevents_model_call():
    storage, request = request_storage()
    storage.sources[SOURCE_PATH] = b"changed"
    model = Model()
    with pytest.raises(AuthoringError, match="SOURCE_INTEGRITY"):
        CandidateAuthoringService(request.request_id, storage, model).author()
    assert model.calls == []
    assert storage.candidates == {}


@pytest.mark.parametrize("code", [
    GOOD + "\ntry:\n    pass\nexcept Exception:\n    return_value = 'fallback'\n",
    GOOD + "\ntry:\n    pass\nexcept:\n    pass\n",
])
def test_authoring_rejects_broad_fallback_handlers(code):
    storage, request = request_storage()
    with pytest.raises(AuthoringError, match="CANDIDATE_CODE_POLICY"):
        CandidateAuthoringService(request.request_id, storage, Model(code)).author()


def test_html_authoring_rejects_read_excel():
    html_source = (b"<html><body><table><tr><th>item</th><th>name</th></tr>"
                   b"<tr><td>A-1</td><td>Widget</td></tr></table></body></html>")
    path = "customer/rfq-id/package-1/original-boms/doc.xls"
    storage = InMemoryStorage(sources={path: html_source})
    profile = profile_bytes(html_source, ".xls")
    request = persist_candidate(storage, "Customer1", "customer-key", profile, ({
        "document_id": "doc1", "blob_path": path, "size_bytes": len(html_source),
        "sha256": __import__("hashlib").sha256(html_source).hexdigest(),
    },))
    code = GOOD + "\nimport pandas as pd\npd.read_excel(args.input)\n"
    with pytest.raises(AuthoringError, match="CANDIDATE_CODE_POLICY") as failure:
        CandidateAuthoringService(request.request_id, storage, Model(code)).author()
    assert failure.value.rule_id == "html_xls_banned_workbook_api"


def test_policy_failure_gets_one_correction_with_stable_rule_id_and_no_code_in_error():
    storage, request = request_storage()
    bad = "import socket\n# --input --output --row-map\n"
    model = CorrectingModel(bad, GOOD)
    result = CandidateAuthoringService(request.request_id, storage, model).author()
    assert result.status == "generated_unvalidated"
    assert len(model.calls) == 2
    assert model.calls[1][2] == bad
    assert model.calls[1][3] == "banned_import"


def test_second_policy_failure_is_not_retried_and_exposes_only_rule_id():
    storage, request = request_storage()
    bad = "import socket\n# --input --output --row-map\n"
    model = CorrectingModel(bad, bad)
    with pytest.raises(AuthoringError) as failure:
        CandidateAuthoringService(request.request_id, storage, model).author()
    assert failure.value.code == "CANDIDATE_CODE_POLICY"
    assert failure.value.rule_id == "banned_import"
    assert bad not in str(failure.value)
    assert len(model.calls) == 2
