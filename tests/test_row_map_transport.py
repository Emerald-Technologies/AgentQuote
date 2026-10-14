"""Row-map transport (plan 8.8 step 4a): worker runtime, remote transfer, job start, validation binding.
Synthetic bytes and fake storage only."""
import base64
import hashlib
import json

import pytest

from bom_processor.job_orchestrator import WorkItem
from worker.runtime import Limits, run_job
from worker.transfer import remote_job
from test_worker_transfer import env, FakeTransport


def parser_bytes(output, row_map=None):
    code = ("import argparse,base64\n"
            "p=argparse.ArgumentParser();p.add_argument('--input');p.add_argument('--output');"
            "p.add_argument('--row-map');a=p.parse_args()\n"
            f"open(a.output,'wb').write(base64.b64decode('{base64.b64encode(output).decode()}'))\n")
    if row_map is not None:
        code += f"open(a.row_map,'wb').write(base64.b64decode('{base64.b64encode(row_map).decode()}'))\n"
    return code.encode()


def test_runtime_passes_row_map_path_and_returns_its_bytes_unverified():
    mapped = b'{"version":"bom-row-map.v1"}'
    ran = run_job(parser_bytes=parser_bytes(b"xlsx-bytes", mapped), input_bytes=b"a,b\n1,2\n",
                  input_name="input.csv", output_name="output.xlsx", output_policy="opaque",
                  return_output=True, row_map_name="row-map.json", limits=Limits(timeout_seconds=10))
    assert ran[1] == 0 and ran[2:] == (b"xlsx-bytes", mapped)
    assert ran[0]["row_map"] == {"sha256": hashlib.sha256(mapped).hexdigest(), "size": len(mapped)}


def test_runtime_fails_when_the_parser_writes_no_row_map():
    ran = run_job(parser_bytes=parser_bytes(b"xlsx-bytes"), input_bytes=b"a,b\n1,2\n",
                  input_name="input.csv", output_name="output.xlsx", output_policy="opaque",
                  return_output=True, row_map_name="row-map.json", limits=Limits(timeout_seconds=10))
    assert ran[1] == 5 and ran[0]["failure"] == "invalid_row_map"


@pytest.mark.parametrize("options", [{"row_map_name": "../x.json"},
                                     {"row_map_name": "row-map.json", "interpretation_name": "interpretation.json"}])
def test_runtime_refuses_unsafe_or_combined_configuration(options):
    with pytest.raises(ValueError):
        run_job(parser_bytes=b"", input_bytes=b"x", input_name="input.csv", output_name="output.xlsx",
                output_policy="opaque", return_output=True, **options)


def test_transfer_uploads_row_map_before_the_completed_result():
    mapped = b'{"version":"bom-row-map.v1"}'
    settings = env(); settings.update(WORKER_ROW_MAP_NAME="row-map.json", ROW_MAP_URL="https://host/row-map?sig=secret")
    def run(**kwargs):
        assert kwargs["row_map_name"] == "row-map.json"
        return {"status": "completed"}, 0, b"xlsx-bytes", mapped
    transport = FakeTransport()
    result, code = remote_job(settings, transport=transport, run=run)
    assert code == 0 and result["contract_version"] == "remote-worker.v3"
    assert [body for _, body, _ in transport.puts][1] == mapped
    assert result["row_map"] == {"sha256": hashlib.sha256(mapped).hexdigest(), "size": len(mapped),
                                 "content_type": "application/json"}
    assert "secret" not in json.dumps(result)


def test_transfer_refuses_partial_row_map_configuration():
    settings = env(); settings["WORKER_ROW_MAP_NAME"] = "row-map.json"
    with pytest.raises(ValueError, match="incomplete row map"):
        remote_job(settings, transport=FakeTransport())


def test_work_item_row_map_is_exclusive_and_cannot_alias_outputs():
    base = ("job", "boms", "parsers", "parsers", "source", "parser", "a" * 64, 1, "b" * 64, 1, "attempt/job")
    assert WorkItem(*base, row_map_name="row-map.json").row_map_blob == "attempt/job/row-map.json"
    with pytest.raises(ValueError, match="invalid row_map_name"):
        WorkItem(*base, row_map_name="../row-map.json")
    with pytest.raises(ValueError, match="exclusive"):
        WorkItem(*base, row_map_name="row-map.json", interpretation_name="interpretation.json")
    with pytest.raises(ValueError, match="collision"):
        WorkItem(*base, result_name="row-map.json", row_map_name="row-map.json")


def test_validation_refuses_a_row_map_that_does_not_match_its_bound_hash():
    from bom_processor.candidate_validation import CandidateValidationError
    from test_candidate_validation import setup_validation
    service, storage, orchestrator, _, _ = setup_validation()
    original = orchestrator.start
    def tamper(item):
        started = original(item)
        storage.candidates[item.row_map_blob] += b" "
        return started
    orchestrator.start = tamper
    with pytest.raises(CandidateValidationError, match="WORKER_ROW_MAP_INTEGRITY"):
        service.validate()


def test_validation_requests_and_records_row_accounting_for_current_candidates():
    from test_candidate_validation import setup_validation
    service, storage, orchestrator, _, _ = setup_validation()
    result = service.validate()
    assert orchestrator.calls[0].row_map_name == "row-map.json"
    evidence = json.loads(storage.candidates[result.evidence_path])
    accounting = evidence["semantic"]["row_accounting"]
    assert accounting["valid"] is True and accounting["source_rows"] == 2 and accounting["output_lines"] == 1
    assert accounting["row_map"]["blob_path"] == orchestrator.calls[0].row_map_blob


def test_candidates_from_older_authoring_versions_are_validated_without_a_row_map():
    # test_package_resolution builds a candidate-authoring.v3 candidate; its worker is never asked
    # for a row map, so approved-era parsers keep today's checks.
    from bom_processor.candidate_authoring import ROW_MAP_AUTHORING_VERSIONS
    assert "candidate-authoring.v3" not in ROW_MAP_AUTHORING_VERSIONS
    assert ROW_MAP_AUTHORING_VERSIONS == {"candidate-authoring.v9", "candidate-authoring.v10"}


def test_production_processing_requests_a_row_map_only_when_provenance_shows_one():
    # Sub-assembly plan R3 (Nick 2026-10-12) replaced "never": legacy variants still run without
    # one, because --row-map would fail a parser that does not accept it (test_reuse_row_check.py).
    import inspect
    from bom_processor import remote
    source = inspect.getsource(remote)
    assert "row_checked = selected is not None and selected.row_map" in source
    assert "row_map_name=ROW_MAP_NAME if row_checked else None" in source
