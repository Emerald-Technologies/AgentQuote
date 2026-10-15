"""Macro-free .xlsm workbooks as BOM sources, read exactly like .xlsx. Offline, synthetic files only.

Each fixture is an openpyxl-written .xlsx whose workbook part is relabelled with the macro-enabled
content type, which is all that separates a macro-free .xlsm from a .xlsx. Covers: the intake rule,
the fail-closed format check, the parse (local and remote worker staging), validation and the row
inventory, registry layout matching, the survey and the name-match pre-pass, and retention.
Run: py -3.12 -m pytest tests/test_source_format.py -q -p no:cacheprovider
"""
import hashlib
import io
import json
import zipfile

import pandas as pd
import pytest
from openpyxl import Workbook

from bom_processor import (BomProcessor, InMemoryStorage, ProcessingError, ProcessingRequest,
                           RemoteBomProcessor)
from bom_processor.candidate_review import _source_counts
from bom_processor.candidate_validation import _source_subtype
from bom_processor.core import CANONICAL_COLUMNS
from bom_processor.foundry_author import build_preview
from bom_processor.interpretation_inventory import InventoryError, extract_inventory
from bom_processor.master_registry import VariantSignature, profile_bytes
from bom_processor.package_evidence_retention import validate_retained_sources
from bom_processor.package_survey import file_kind
from bom_processor.row_accounting import verify_row_map
from bom_processor.source_format import (FORMAT_VERSION, SourceFormatError, check_source, is_macro_enabled,
                                         reading_extension, staged_input_name)

XLSX_MAIN = b"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"
XLSM_MAIN = b"application/vnd.ms-excel.sheet.macroEnabled.main+xml"
RFQ = "12345678-1234-4234-8234-123456789012"
PREFIX = f"cust/rfq-{RFQ}/package-v1/original-boms"


def relabel(data, content_type=XLSM_MAIN, *, vba=False, drop=()):
    """Rewrite an .xlsx with another workbook content type (and optionally a VBA part)."""
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as source, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as target:
        for item in source.infolist():
            if item.filename in drop:
                continue
            part = source.read(item)
            if item.filename == "[Content_Types].xml":
                assert XLSX_MAIN in part
                part = part.replace(XLSX_MAIN, content_type)
            target.writestr(item.filename, part)
        if vba:
            target.writestr("xl/vbaProject.bin", b"\x00synthetic-not-a-real-macro")
    return out.getvalue()


def sheet_bytes(rows, title="BOM"):
    book = Workbook(); sheet = book.active; sheet.title = title
    for row in rows:
        sheet.append(row)
    stream = io.BytesIO(); book.save(stream); book.close()
    return stream.getvalue()


def xlsm(data, **options):
    return relabel(data, **options)


def output_workbook(**changes):
    row = {column: None for column in CANONICAL_COLUMNS}
    row.update(line_number=1, item_number="P1", item_name="Widget", item_category="Part", quantity=2.0)
    row.update(changes)
    stream = io.BytesIO()
    pd.DataFrame([row], columns=CANONICAL_COLUMNS).to_excel(stream, index=False)
    return stream.getvalue()


BOM_ROWS = [["Item", "Part Number", "Manufacturer", "MPN", "Qty"],
            [1, "P-1", "Maker", "M-1", 2], [2, "P-2", "Maker", "M-2", 1]]
XLSX = sheet_bytes(BOM_ROWS)
XLSM = xlsm(XLSX)


# --- names, extensions and the fixture itself ---------------------------------------------------

def test_the_fixture_is_a_macro_enabled_package_without_macros():
    with zipfile.ZipFile(io.BytesIO(XLSM)) as archive:
        assert XLSM_MAIN in archive.read("[Content_Types].xml")
        assert XLSX_MAIN not in archive.read("[Content_Types].xml")
        assert not [name for name in archive.namelist() if "vba" in name.casefold()]


@pytest.mark.parametrize("value", [".xlsm", ".XLSM", "xlsm", "doc.xlsm", f"{PREFIX}/doc1-BOM Rev A.XLSM"])
def test_every_spelling_of_xlsm_reads_and_stages_as_xlsx(value):
    assert is_macro_enabled(value)
    assert reading_extension(value) == ".xlsx"


@pytest.mark.parametrize("value, expected", [(".xlsx", ".xlsx"), ("xls", ".xls"), ("a/b.CSV", ".csv"),
                                             ("doc.xlsb", ".xlsb"), ("", ""), ("a/no-extension", "")])
def test_other_extensions_are_unchanged(value, expected):
    assert not is_macro_enabled(value)
    assert reading_extension(value) == expected


def test_staged_input_names():
    assert staged_input_name(f"{PREFIX}/doc1-bom.xlsm") == "input.xlsx"
    assert staged_input_name(f"{PREFIX}/doc1-bom.XLSX") == "input.xlsx"
    assert staged_input_name(f"{PREFIX}/doc1-bom.xls") == "input.xls"
    assert staged_input_name(f"{PREFIX}/doc1-bom.csv") == "input.csv"


# --- the fail-closed format check ---------------------------------------------------------------

def test_a_macro_free_xlsm_passes_with_provenance():
    assert check_source(f"{PREFIX}/doc1-bom.xlsm", XLSM) == {
        "version": FORMAT_VERSION, "extension": ".xlsm", "read_as": ".xlsx",
        "workbook_content_type": XLSM_MAIN.decode(), "vba_project_present": False, "macros_run": False}
    # A plain worksheet saved under the .xlsm name is macro-free too.
    assert check_source("doc.xlsm", XLSX)["workbook_content_type"] == XLSX_MAIN.decode()


def test_other_formats_are_not_checked_here():
    assert check_source("doc.xlsx", b"not even a zip") is None
    assert check_source("doc.csv", b"a,b\n") is None


@pytest.mark.parametrize("data, code", [
    (xlsm(XLSX, vba=True), "SOURCE_WORKBOOK_HAS_MACROS"),
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64, "SOURCE_WORKBOOK_UNREADABLE"),  # legacy .xls renamed
    (b"PK\x03\x04 truncated", "SOURCE_WORKBOOK_UNREADABLE"),
    (XLSM[: len(XLSM) // 2], "SOURCE_WORKBOOK_UNREADABLE"),
    (b"", "SOURCE_WORKBOOK_UNREADABLE"),
    (relabel(XLSX, b"application/vnd.ms-excel.template.macroEnabled.main+xml"), "SOURCE_WORKBOOK_CONTENT_TYPE"),
    (relabel(XLSX, b"application/vnd.ms-excel.addin.macroEnabled.main+xml"), "SOURCE_WORKBOOK_CONTENT_TYPE"),
    (relabel(XLSX, drop=("xl/workbook.xml",)), "SOURCE_WORKBOOK_UNREADABLE"),
    (relabel(XLSX, drop=("xl/worksheets/sheet1.xml",)), "SOURCE_WORKBOOK_UNREADABLE"),
])
def test_an_xlsm_that_is_not_a_readable_macro_free_worksheet_is_refused(data, code):
    with pytest.raises(SourceFormatError) as error:
        check_source("doc.xlsm", data)
    assert error.value.code == code


def test_a_vba_part_anywhere_in_the_package_is_refused():
    with zipfile.ZipFile(io.BytesIO(XLSM)) as source:
        parts = {name: source.read(name) for name in source.namelist()}
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as target:
        for name, part in parts.items():
            target.writestr(name, part)
        target.writestr("customXml/VBAData.xml", b"<x/>")
    with pytest.raises(SourceFormatError) as error:
        check_source("doc.xlsm", out.getvalue())
    assert error.value.code == "SOURCE_WORKBOOK_HAS_MACROS"


# --- intake and the parse -----------------------------------------------------------------------

def request(path, data, *, job_id="job1"):
    return ProcessingRequest.from_dict({
        "job_id": job_id, "customer_key": "cust", "parser_key": "Parser1", "rfq_uuid": RFQ,
        "package_version": "v1", "documents": [{"document_id": "doc1", "blob_path": path,
                                                "size_bytes": len(data),
                                                "sha256": hashlib.sha256(data).hexdigest()}]})


def test_intake_accepts_xlsm_and_still_refuses_other_macro_or_binary_formats():
    request(f"{PREFIX}/doc1-bom.xlsm", XLSM).validate()
    request(f"{PREFIX}/doc1-bom.XLSM", XLSM).validate()
    for suffix in (".xlsb", ".xltm", ".xlam", ".docm"):
        with pytest.raises(ProcessingError, match="invalid source path"):
            request(f"{PREFIX}/doc1-bom{suffix}", XLSM)


# A real parse: the parser opens the workbook it is given with openpyxl and must be handed input.xlsx.
READING_PARSER = b"""import argparse, os, openpyxl
parser = argparse.ArgumentParser()
parser.add_argument('--input', required=True)
parser.add_argument('--output', required=True)
args = parser.parse_args()
assert os.path.basename(args.input) == 'input.xlsx', args.input
book = openpyxl.load_workbook(args.input, data_only=True)
book.save(args.output)
"""


def test_local_processing_parses_an_xlsm_as_input_xlsx_and_records_the_format():
    source = xlsm(output_workbook(item_name="Widget from xlsm"))
    path = f"{PREFIX}/doc1-bom.xlsm"
    storage = InMemoryStorage(sources={path: source}, parsers={"Parser1/parse.py": READING_PARSER})
    manifest = BomProcessor(storage).process(request(path, source))
    result = manifest["results"][0]
    assert result["source"]["blob_path"] == path  # the original name and hash stay the provenance
    assert result["source"]["sha256"] == hashlib.sha256(source).hexdigest()
    assert result["source_format"]["extension"] == ".xlsm" and result["source_format"]["macros_run"] is False
    parsed = pd.read_excel(io.BytesIO(storage.outputs[result["output"]["blob_path"]]))
    assert parsed["item_name"].tolist() == ["Widget from xlsm"]


def test_an_xlsx_manifest_is_unchanged():
    source = output_workbook()
    path = f"{PREFIX}/doc1-bom.xlsx"
    storage = InMemoryStorage(sources={path: source}, parsers={"Parser1/parse.py": READING_PARSER})
    result = BomProcessor(storage).process(request(path, source))["results"][0]
    assert set(result) == {"source", "output"}


@pytest.mark.parametrize("data, message", [
    (xlsm(output_workbook(), vba=True), "source workbook contains macros"),
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64, "source workbook unreadable"),
    (relabel(output_workbook(), b"application/vnd.ms-excel.template.macroEnabled.main+xml"),
     "source workbook type unsupported"),
])
def test_local_processing_fails_closed_before_any_parser_runs(data, message):
    path = f"{PREFIX}/doc1-bom.xlsm"
    storage = InMemoryStorage(sources={path: data},
                              parsers={"Parser1/parse.py": b"raise SystemExit('the parser must not run')\n"})
    with pytest.raises(ProcessingError, match=message):
        BomProcessor(storage).process(request(path, data))
    assert storage.outputs == {}


def remote(path, source, output):
    from test_remote_processor import Orchestrator
    storage = InMemoryStorage(sources={path: source}, parsers={"Parser1/parse.py": b"approved parser"})
    orchestration = Orchestrator(storage, output)
    return RemoteBomProcessor(storage, orchestration, sleep=lambda _: None), storage, orchestration


def test_the_remote_worker_is_given_the_xlsm_bytes_as_input_xlsx():
    source, path = xlsm(output_workbook()), f"{PREFIX}/doc1-bom.xlsm"
    processor, storage, orchestration = remote(path, source, output_workbook())
    manifest = processor.process(request(path, source))
    item = orchestration.calls[0]
    assert item.input_name == "input.xlsx"  # the worker image's existing input names are unchanged
    assert item.source_blob == path and item.input_sha256 == hashlib.sha256(source).hexdigest()
    assert manifest["results"][0]["source_format"]["read_as"] == ".xlsx"
    stored = json.loads(storage.outputs[f"cust/rfq-{RFQ}/package-v1/processed/job1/manifest.json"])
    assert stored["results"][0]["source_format"]["extension"] == ".xlsm"


def test_the_remote_processor_fails_closed_before_starting_a_worker():
    source, path = xlsm(output_workbook(), vba=True), f"{PREFIX}/doc1-bom.xlsm"
    processor, storage, orchestration = remote(path, source, output_workbook())
    with pytest.raises(ProcessingError, match="source workbook contains macros"):
        processor.process(request(path, source))
    assert orchestration.calls == []


# --- reading: inventory, row accounting, profile, previews ---------------------------------------

def test_the_row_inventory_reads_an_xlsm_exactly_like_the_xlsx():
    plain, macro_enabled = extract_inventory(XLSX, ".xlsx"), extract_inventory(XLSM, ".xlsm")
    assert macro_enabled.cells == plain.cells and macro_enabled.sheets == plain.sheets == ("BOM",)
    assert macro_enabled.sha256 == hashlib.sha256(XLSM).hexdigest()


def test_the_row_inventory_still_refuses_active_content():
    with pytest.raises(InventoryError, match="SOURCE_ACTIVE_OR_EXTERNAL_CONTENT"):
        extract_inventory(xlsm(XLSX, vba=True), ".xlsm")


def test_row_accounting_verifies_a_row_map_against_an_xlsm_source():
    from test_row_accounting import good
    source, out, row_map = good()
    result = verify_row_map(xlsm(source), ".xlsm", out, json.dumps(row_map).encode())
    assert result["valid"] is True and result["failed_rows"] == []


def test_an_xlsm_profiles_as_xlsx_and_matches_a_layout_approved_from_an_xlsx():
    plain, macro_enabled = profile_bytes(XLSX, ".xlsx"), profile_bytes(XLSM, ".xlsm")
    assert macro_enabled.extension == ".xlsx" and macro_enabled.source_subtype == "xlsx"
    assert macro_enabled.profile_hash == plain.profile_hash
    assert VariantSignature.from_profile(plain.to_dict()).matches(macro_enabled)
    assert VariantSignature.from_profile(plain.to_dict(), heading=True).matches(macro_enabled)
    # Candidate validation runs the xlsx document-role check for it.
    assert _source_subtype({"profile": macro_enabled.to_dict()}) == "xlsx"


def test_author_preview_and_review_counts_read_an_xlsm():
    preview = build_preview(XLSM, ".xlsm")
    assert "P-1" in preview and "<unsupported format>" not in preview and "<unreadable workbook>" not in preview
    assert build_preview(XLSM, "xlsm") == build_preview(XLSX, "xlsx")
    assert _source_counts(XLSM, ".xlsm") == _source_counts(XLSX, ".xlsx")
    assert _source_counts(XLSM, ".xlsm")["line_items"] is not None


# --- candidate authoring and validation ---------------------------------------------------------

def test_a_candidate_for_an_xlsm_is_authored_for_xlsx_and_validated_on_input_xlsx():
    from bom_processor.candidate_authoring import CandidateAuthoringService
    from bom_processor.candidate_validation import CandidateValidationService
    from bom_processor.candidates import persist_candidate
    from test_candidate_validation import CODE, Orchestrator, workbook

    source = xlsm(sheet_bytes([["mystery", "name", "qty", "maker", "maker part"],
                               ["P1", "Widget", 2, "Acme", "A-100"]]))
    path = "customer/rfq-id/package-1/original-boms/doc.xlsm"
    storage = InMemoryStorage(sources={path: source})
    request_record = persist_candidate(storage, "Customer1", "customer-key", profile_bytes(source, ".xlsm"), ({
        "document_id": "doc1", "blob_path": path, "size_bytes": len(source),
        "sha256": hashlib.sha256(source).hexdigest()},))

    class Model:
        def generate(self, prompt, source_bytes):
            self.extension = prompt.source_analysis.extension
            return CODE

    model = Model()
    candidate = CandidateAuthoringService(request_record.request_id, storage, model).author()
    assert model.extension == "xlsx"  # the author writes for the input.xlsx it will be given
    orchestrator = Orchestrator(storage, workbook())
    result = CandidateValidationService(
        request_record.request_id, candidate.candidate_id, storage, orchestrator,
        job_resource="subscriptions/sub/resourceGroups/rg/providers/Microsoft.App/jobs/worker",
        worker_image="registry/worker@sha256:" + "a" * 64, sleep=lambda _: None).validate()
    assert orchestrator.calls[0].input_name == "input.xlsx"
    assert orchestrator.calls[0].source_blob == path
    assert result.status == "awaiting_approval"


# --- package survey, name-match pre-pass, retention ---------------------------------------------

def test_the_survey_types_an_xlsm_as_an_ooxml_spreadsheet():
    assert file_kind("BOM 0100-11111 REV 01.xlsm", XLSM) == "spreadsheet_ooxml"
    assert file_kind("folder/BOM.XLSM", XLSM) == "spreadsheet_ooxml"


def test_the_name_match_pre_pass_maps_a_loose_xlsm_bom():
    from bom_processor.bom_package_choice import decide
    from test_package_name_match import BOM_1, BOM_2, PCB_1, PCB_2, A1, A2, decisions, no_model, request as choice_request

    bom_1 = xlsm(BOM_1)
    files = [("BOM 0100-11111 REV 01.xlsm", bom_1), ("BOM 0100-22222 REV 03.xlsx", BOM_2),
             ("XYZ0100-11111-TK.zip", PCB_1), ("XYZ0100-22222-TK.zip", PCB_2)]
    result = decide(choice_request({"doc-1": bom_1, "doc-2": BOM_2}), files, no_model)
    assert result["model_calls"] == 0
    assert decisions(result) == {"doc-1": ("process", "only_bom", [A1]), "doc-2": ("process", "only_bom", [A2])}
    records = {item["path"]: item for item in result["survey"]["files"]}
    assert records["BOM 0100-11111 REV 01.xlsm"]["requested_assembly"]["link"] == "confirmed"


def test_an_original_xlsm_bom_is_a_retained_source():
    from test_package_evidence_inventory import root
    original = root(XLSM, "BOM Rev A.xlsm", "12345678-1234-4234-8234-123456789081")
    proof = {"requestFingerprint": "e" * 64, "stagingEventId": "12345678-1234-4234-8234-123456789080",
             "sources": [{key: original[key] for key in ("documentId", "sha256", "sizeBytes")}]}
    assert validate_retained_sources(proof, [original]) == {original["documentId"]}
