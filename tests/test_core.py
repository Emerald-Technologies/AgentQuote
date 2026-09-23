import hashlib
import json
from io import BytesIO

import pandas as pd
import pytest

from bom_processor import (
    BlobConflictError,
    BomProcessor,
    InMemoryStorage,
    ProcessingError,
    ProcessingRequest,
)

RFQ = "12345678-1234-4234-8234-123456789012"
PREFIX = f"cust/rfq-{RFQ}/package-v1/original-boms"
COLUMNS = [
    "line_number", "item_number", "item_name", "revision", "item_category",
    "unit_of_measure", "quantity", "reference_designator", "bom_notes",
] + [column for index in range(1, 9) for column in (
    f"manufacturer_{index}", f"manufacturer_item_number_{index}"
)]
COPY_PARSER = b"""import argparse, shutil
parser = argparse.ArgumentParser()
parser.add_argument('--input', required=True)
parser.add_argument('--output', required=True)
args = parser.parse_args()
shutil.copyfile(args.input, args.output)
"""
FAIL_PARSER = b"raise RuntimeError('expected failure')\n"
TIMEOUT_PARSER = b"import time\ntime.sleep(10)\n"


def workbook(**changes) -> bytes:
    row = {
        "line_number": 1, "item_number": "P1", "item_name": "Widget",
        "revision": "A", "item_category": "Part", "unit_of_measure": "EA",
        "quantity": 2.0, "reference_designator": None, "bom_notes": None,
    }
    for index in range(1, 9):
        row[f"manufacturer_{index}"] = None
        row[f"manufacturer_item_number_{index}"] = None
    row.update(changes)
    stream = BytesIO()
    pd.DataFrame([row], columns=COLUMNS).to_excel(stream, index=False)
    return stream.getvalue()


def request(blob: bytes, *, job_id="job1", path=None, size=None, sha=None):
    source_path = path or f"{PREFIX}/doc1-input.xlsx"
    return ProcessingRequest.from_dict({
        "job_id": job_id, "customer_key": "cust", "parser_key": "Astronics",
        "rfq_uuid": RFQ, "package_version": "v1",
        "documents": [{
            "document_id": "doc1", "blob_path": source_path,
            "size_bytes": len(blob) if size is None else size,
            "sha256": hashlib.sha256(blob).hexdigest() if sha is None else sha,
        }],
    })


def storage(blob: bytes, parser=COPY_PARSER) -> InMemoryStorage:
    return InMemoryStorage(
        sources={f"{PREFIX}/doc1-input.xlsx": blob},
        parsers={"Astronics/parse.py": parser},
    )


def test_happy_path_publishes_provenance_manifest_and_exact_output():
    blob = workbook()
    store = storage(blob)
    result = BomProcessor(store).process(request(blob))
    output_path = f"cust/rfq-{RFQ}/package-v1/processed/job1/doc1-parsed.xlsx"
    manifest_path = f"cust/rfq-{RFQ}/package-v1/processed/job1/manifest.json"

    assert result["status"] == "completed"
    assert result["parser"]["key"] == "Astronics"
    assert result["results"][0]["source"] == {
        "document_id": "doc1", "blob_path": f"{PREFIX}/doc1-input.xlsx",
        "size_bytes": len(blob), "sha256": hashlib.sha256(blob).hexdigest(),
    }
    assert result["results"][0]["output"]["blob_path"] == output_path
    assert store.outputs[output_path] == blob
    assert json.loads(store.outputs[manifest_path]) == result
    assert store.output_content_types[manifest_path] == "application/json"


def test_matching_replay_returns_manifest_without_parser_or_source():
    blob = workbook()
    store = storage(blob)
    processor = BomProcessor(store)
    first = processor.process(request(blob))
    store.sources.clear()
    store.parsers.clear()
    assert processor.process(request(blob)) == first


def test_replay_rejects_missing_or_changed_completed_output():
    blob = workbook()
    output_path = f"cust/rfq-{RFQ}/package-v1/processed/job1/doc1-parsed.xlsx"
    for replacement in (None, b"changed"):
        store = storage(blob)
        processor = BomProcessor(store)
        processor.process(request(blob))
        if replacement is None:
            del store.outputs[output_path]
        else:
            store.outputs[output_path] = replacement
        with pytest.raises(BlobConflictError, match="missing or changed"):
            processor.process(request(blob))


def test_same_job_with_different_request_is_rejected():
    blob = workbook()
    store = storage(blob)
    processor = BomProcessor(store)
    processor.process(request(blob))
    changed = request(blob, sha="0" * 64)
    with pytest.raises(BlobConflictError, match="different request"):
        processor.process(changed)


@pytest.mark.parametrize("path", [
    f"{PREFIX}/../x.xlsx",
    f"{PREFIX}/subdir/x.xlsx",
    f"{PREFIX}\\x.xlsx",
    f"other/rfq-{RFQ}/package-v1/original-boms/x.xlsx",
    f"{PREFIX}/x.exe",
])
def test_invalid_source_paths_are_rejected(path):
    with pytest.raises(ProcessingError, match="source path"):
        request(b"x", path=path)


def test_malformed_request_and_boolean_size_are_rejected():
    with pytest.raises(ProcessingError, match="invalid request"):
        ProcessingRequest.from_dict([])
    with pytest.raises(ProcessingError, match="source metadata"):
        request(b"x", size=True)


@pytest.mark.parametrize("size,sha", [(1, None), (None, "0" * 64)])
def test_source_integrity_mismatch_is_rejected(size, sha):
    blob = workbook()
    with pytest.raises(ProcessingError, match="integrity"):
        BomProcessor(storage(blob)).process(request(blob, size=size, sha=sha))


def test_per_file_and_aggregate_limits_are_checked_before_execution():
    blob = workbook()
    with pytest.raises(ProcessingError, match="source exceeds"):
        BomProcessor(storage(blob), max_source_bytes=10).process(request(blob))
    with pytest.raises(ProcessingError, match="aggregate"):
        BomProcessor(storage(blob), max_total_source_bytes=10).process(request(blob))


@pytest.mark.parametrize("parser,error", [
    (FAIL_PARSER, "parser failed"),
    (TIMEOUT_PARSER, "parser timed out"),
])
def test_parser_failure_and_timeout_are_safe(parser, error):
    blob = workbook()
    processor = BomProcessor(storage(blob, parser), timeout_seconds=1)
    with pytest.raises(ProcessingError, match=error):
        processor.process(request(blob))


@pytest.mark.parametrize("changes,error", [
    ({"item_category": "Assembly"}, "item category"),
    ({"line_number": 2}, "line numbers"),
    ({"item_name": " Widget"}, "string value"),
    ({"quantity": float("inf")}, "quantity"),
    ({"manufacturer_1": "Acme"}, "manufacturer pair"),
])
def test_strict_schema_validation(changes, error):
    blob = workbook(**changes)
    with pytest.raises(ProcessingError, match=error):
        BomProcessor(storage(blob)).process(request(blob))


def test_existing_conflicting_output_fails_closed():
    blob = workbook()
    output_path = f"cust/rfq-{RFQ}/package-v1/processed/job1/doc1-parsed.xlsx"
    store = storage(blob)
    store.outputs[output_path] = b"different"
    with pytest.raises(BlobConflictError, match="immutable output conflict"):
        BomProcessor(store).process(request(blob))


def test_all_documents_validate_before_any_output_is_published():
    good = workbook()
    bad = b"not-a-workbook"
    second_path = f"{PREFIX}/doc2-input.xlsx"
    store = InMemoryStorage(
        sources={f"{PREFIX}/doc1-input.xlsx": good, second_path: bad},
        parsers={"Astronics/parse.py": COPY_PARSER},
    )
    value = {
        "job_id": "job1", "customer_key": "cust", "parser_key": "Astronics",
        "rfq_uuid": RFQ, "package_version": "v1", "documents": [
            {"document_id": "doc1", "blob_path": f"{PREFIX}/doc1-input.xlsx",
             "size_bytes": len(good), "sha256": hashlib.sha256(good).hexdigest()},
            {"document_id": "doc2", "blob_path": second_path,
             "size_bytes": len(bad), "sha256": hashlib.sha256(bad).hexdigest()},
        ],
    }
    with pytest.raises(ProcessingError, match="workbook"):
        BomProcessor(store).process(ProcessingRequest.from_dict(value))
    assert store.outputs == {}
