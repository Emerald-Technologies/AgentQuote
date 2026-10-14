"""Row check when an approved parser is reused (sub-assembly plan R3, Nick 2026-10-12).

Promotion records provenance beside the approval; reuse asks for a row map only when the
provenance says the parser was validated with one, checks it, and carries the result as a warning
(never a stop). Variants approved before provenance existed are reused unchecked and say so.
Synthetic data only."""
import hashlib
import json
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from bom_processor import InMemoryStorage, ProcessingError, RemoteBomProcessor, StartResult
from bom_processor import variant_provenance
from bom_processor.blob_registry import BlobRegistryReader, RegistrySelection
from bom_processor.master_registry import RegistryError
from test_admin_promotion import SOURCE, prepared
from test_blob_registry import CUSTOMER, PARSER_KEY, fixture_blobs, reader
from test_blob_registry import workbook as registry_workbook
from test_remote_processor import PARSER, SOURCE_PATH, request, workbook

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def test_promotion_records_provenance_and_reuse_asks_for_a_row_map():
    storage, service, common = prepared()
    service.decide(decision="approve", variant_id="mystery-csv", version="1", **common)
    record = json.loads(storage.registry["approved/Customer1/mystery-csv/1/provenance.json"])
    assert record["version"] == "variant-provenance.v1" and record["row_map"] is True
    assert record["authoring_version"].startswith("candidate-authoring.v")
    selected = BlobRegistryReader(storage.download_registry).select("Customer1", "customer-key", SOURCE, ".csv")
    assert selected.row_map is True
    # A replayed promotion writes identical provenance bytes (immutable blobs accept them).
    service.decide(decision="approve", variant_id="mystery-csv", version="1", **common)


def test_a_variant_approved_before_provenance_is_reused_without_a_row_map():
    selected = reader(fixture_blobs()).select(PARSER_KEY, CUSTOMER, registry_workbook(), ".xlsx")
    assert selected.row_map is False


def provenance_for(blobs, **changes):
    approval = json.loads(blobs[f"approved/{PARSER_KEY}/format1/1/approval.json"])
    record = {"version": "variant-provenance.v1", "approval_id": approval["approval_id"], "request_id": "r1",
              "candidate_id": approval["candidate_id"], "validation_id": "v1",
              "authoring_version": "candidate-authoring.v9",
              "candidate_manifest_sha256": approval["candidate_hash"], "row_map": True}
    record.update(changes)
    return {**blobs, f"approved/{PARSER_KEY}/format1/1/provenance.json": json.dumps(record).encode()}


def test_provenance_must_belong_to_the_approval():
    blobs = fixture_blobs()
    assert reader(provenance_for(blobs)).select(PARSER_KEY, CUSTOMER, registry_workbook(), ".xlsx").row_map is True
    for changes in ({"candidate_manifest_sha256": "9" * 64}, {"approval_id": "other"}, {"row_map": "yes"},
                    {"extra": 1}):
        with pytest.raises(RegistryError, match="REGISTRY_CORRUPT"):
            reader(provenance_for(blobs, **changes)).select(PARSER_KEY, CUSTOMER, registry_workbook(), ".xlsx")


def test_provenance_row_map_needs_a_row_map_authoring_version_and_passed_row_checks():
    manifest = {"version": "candidate-authoring.v9"}
    passed = {"semantic": {"row_accounting": {"valid": True}}}
    common = dict(approval_id="a", request_id="r", candidate_id="c", validation_id="v",
                  candidate_manifest_sha256="0" * 64)
    assert variant_provenance.build(manifest, passed, **common)["row_map"] is True
    assert variant_provenance.build({"version": "candidate-authoring.v3"}, passed, **common)["row_map"] is False
    assert variant_provenance.build(manifest, {"semantic": {"row_accounting": {"valid": False}}}, **common)["row_map"] is False
    grouped = {"fixtures": [{"fixture_sha256": "1", **passed}, {"fixture_sha256": "2", "semantic": {}}]}
    assert variant_provenance.build(manifest, grouped, **common)["row_map"] is False


class Selector:
    def __init__(self, row_map):
        from test_blob_registry import fixture_blobs as blobs
        base = reader(blobs()).select(PARSER_KEY, CUSTOMER, registry_workbook(), ".xlsx")
        self.selection = replace(base, row_map=row_map)

    def select(self, *args):
        return self.selection


class Worker:
    """Writes the parser output and, when asked, a row map citing the source workbook."""
    def __init__(self, storage, output, row_map, *, tamper=False):
        self.storage, self.output, self.row_map, self.tamper = storage, output, row_map, tamper
        self.calls = []

    def start(self, item):
        self.calls.append(item)
        self.storage.outputs[item.output_blob] = self.output
        result = {"contract_version": "remote-worker.v3" if item.row_map_name else "remote-worker.v1",
                  "job": item.job_id, "status": "completed",
                  "input": {"verified": True, "sha256": item.input_sha256, "size": item.input_size},
                  "parser": {"verified": True, "sha256": item.parser_sha256, "size": item.parser_size},
                  "output": {"sha256": hashlib.sha256(self.output).hexdigest(), "size": len(self.output),
                             "content_type": XLSX}}
        if item.row_map_name:
            self.storage.outputs[item.row_map_blob] = self.row_map + (b" " if self.tamper else b"")
            result["row_map"] = {"sha256": hashlib.sha256(self.row_map).hexdigest(), "size": len(self.row_map),
                                 "content_type": "application/json"}
        self.storage.outputs[item.result_blob] = json.dumps(result).encode()
        return StartResult(item.job_id, "execution1", datetime.now(timezone.utc))


def row_map(item_cell_value_matches=True):
    # Source and output are the same one-line canonical workbook; the map cites its own cells.
    fields = {"item_number": [2, 2], "item_name": [2, 3], "quantity": [2, 7]}
    if not item_cell_value_matches:
        fields["item_name"] = [2, 2]  # cites the part number cell for the name: a mismatch
    return json.dumps({"version": "bom-row-map.v2", "lines": [
        {"output_row": 2, "sheet": "Sheet1", "rows": [2], "fields": fields}],
        "skipped": [{"sheet": "Sheet1", "rows": [[1, 1]], "reason": "header", "rationale": "Headings."}],
        "ignored_columns": [{"sheet": "Sheet1", "column": 1, "reason": "Line number."},
                            {"sheet": "Sheet1", "column": 5, "reason": "Category."}]}).encode()


def run(selection_row_map, mapped, **worker_options):
    source = workbook()
    storage = InMemoryStorage(sources={SOURCE_PATH: source}, parsers={"Parser1/parse.py": PARSER})
    worker = Worker(storage, source, mapped, **worker_options)
    remote = RemoteBomProcessor(storage, worker, sleep=lambda _: None,
                                registry_reader=Selector(selection_row_map))
    return remote.process(request(source)), worker


def test_reuse_with_provenance_asks_for_and_checks_a_row_map():
    manifest, worker = run(True, row_map())
    assert worker.calls[0].row_map_name == "row-map.json"
    accounting = manifest["results"][0]["row_accounting"]
    assert accounting["checked"] is True and accounting["valid"] is True, accounting
    assert accounting["row_map"]["blob_path"] == worker.calls[0].row_map_blob
    assert "row_accounting" not in manifest["results"][0]["parser"]


def test_a_failed_row_check_on_reuse_is_a_warning_and_the_output_is_kept():
    manifest, _ = run(True, row_map(item_cell_value_matches=False))
    assert manifest["status"] == "completed"
    accounting = manifest["results"][0]["row_accounting"]
    assert accounting["valid"] is False and "FIELD_VALUE_MISMATCH" in accounting["failure_codes"]
    assert "Widget" not in json.dumps(accounting)  # counts and codes only


def test_a_tampered_row_map_is_still_a_hard_failure():
    with pytest.raises(ProcessingError, match="row map hash mismatch"):
        run(True, row_map(), tamper=True)


def test_reuse_without_provenance_is_unchecked_and_says_so():
    manifest, worker = run(False, row_map())
    assert worker.calls[0].row_map_name is None
    assert manifest["results"][0]["row_accounting"] == {"checked": False, "reason": "PARSER_APPROVED_WITHOUT_ROW_MAP"}
