"""Optional package_files in the processing request (plan 8.8 step 2). Offline, synthetic."""
import copy

import pytest

from bom_processor import ProcessingError, ProcessingRequest

RFQ = "11111111-2222-4333-8444-555555555555"
ZIP_ID = "2222abcd-3333-4444-8555-666666666666"
PDF_ID = "33333333-4444-4555-8666-777777777777"


def base():
    return {"job_id": "qq-0123456789abcdef0123456789abcdef", "customer_key": "internal-abc",
            "parser_key": "internal-abc", "rfq_uuid": RFQ, "package_version": "1",
            "documents": [{"document_id": "doc-1", "size_bytes": 10, "sha256": "a" * 64,
                           "blob_path": f"internal-abc/rfq-{RFQ}/package-1/original-boms/doc-1-bom.xlsx"}]}


def package_file(document_id, name, digest, size=20):
    return {"document_id": document_id, "file_name": name, "size_bytes": size, "sha256": digest,
            "blob_path": f"evidence-inputs/{RFQ}/package-1/{document_id}/{digest}"}


def with_files():
    value = base()
    value["package_files"] = [package_file(ZIP_ID, "RFQ PACKAGE.zip", "b" * 64),
                              package_file(PDF_ID, "drawing.pdf", "c" * 64)]
    return value


def test_request_without_package_files_keeps_its_fingerprint():
    # Golden value computed before package_files existed (2026-10-08).
    request = ProcessingRequest.from_dict(base())
    assert request.package_files is None
    assert request.fingerprint() == "ad166dfe9b6654d21bce36362bc105f27743d17159804814626a2340b741b166"


def test_package_files_are_parsed_and_bound_to_the_fingerprint():
    request = ProcessingRequest.from_dict(with_files())
    assert [item.file_name for item in request.package_files] == ["RFQ PACKAGE.zip", "drawing.pdf"]
    # Shared with QuickQuote tests/unit/agentquote-package-files.test.ts: both languages must agree.
    assert request.fingerprint() == "0632f6fd846f14b0affa797b5f5beb04b79910e7a529485628aebe1edb336b38"
    assert request.fingerprint() != ProcessingRequest.from_dict(base()).fingerprint()
    renamed = with_files()
    renamed["package_files"][1]["file_name"] = "other.pdf"
    assert ProcessingRequest.from_dict(renamed).fingerprint() != request.fingerprint()


@pytest.mark.parametrize("change", [
    lambda v: v.update(package_files=[]),
    lambda v: v.update(package_files="x"),
    lambda v: v["package_files"][0].update(extra=1),
    lambda v: v["package_files"][0].pop("file_name"),
    lambda v: v["package_files"].reverse(),
    lambda v: v["package_files"].append(copy.deepcopy(v["package_files"][0])),
    lambda v: v["package_files"][0].update(document_id=ZIP_ID.upper()),
    lambda v: v["package_files"][0].update(file_name="a/b.zip"),
    lambda v: v["package_files"][0].update(file_name="a\\b.zip"),
    lambda v: v["package_files"][0].update(file_name="C:x.zip"),
    lambda v: v["package_files"][0].update(file_name=" "),
    lambda v: v["package_files"][0].update(file_name="x" * 257),
    lambda v: v["package_files"][0].update(size_bytes=100_000_001),
    lambda v: v["package_files"][0].update(size_bytes=True),
    lambda v: v["package_files"][0].update(sha256="B" * 64),
    lambda v: v["package_files"][0].update(blob_path=f"evidence-inputs/{RFQ}/package-1/{ZIP_ID}/{'d' * 64}"),
    lambda v: v["package_files"][0].update(blob_path=f"evidence-inputs/{RFQ}/package-2/{ZIP_ID}/{'b' * 64}"),
    lambda v: v["package_files"][0].update(blob_path=f"evidence-inputs/{PDF_ID}/package-1/{ZIP_ID}/{'b' * 64}"),
    lambda v: v["package_files"][0].update(blob_path=f"internal-abc/rfq-{RFQ}/package-1/original-boms/x.zip"),
])
def test_package_files_are_strictly_validated(change):
    value = with_files()
    change(value)
    with pytest.raises(ProcessingError):
        ProcessingRequest.from_dict(value)


def test_package_files_total_is_limited():
    value = base()
    ids = [f"{index:08x}-3333-4444-8555-666666666666" for index in range(6)]
    value["package_files"] = [package_file(i, f"f{n}.zip", f"{n:x}" * 64, size=100_000_000)
                              for n, i in enumerate(ids, 1)]
    with pytest.raises(ProcessingError):
        ProcessingRequest.from_dict(value)
    value["package_files"] = value["package_files"][:5]
    assert len(ProcessingRequest.from_dict(value).package_files) == 5
