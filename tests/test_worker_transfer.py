import hashlib
import json
import os
from unittest.mock import patch

from worker.__main__ import main
from worker.transfer import remote_job


PARSER = b"parser"
INPUT = b"input"
OUTPUT = b'{"rows":[],"schema":"agentquote.synthetic.v1"}'
OUTPUT_NAME = "output.xlsx"
OUTPUT_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class FakeTransport:
    def __init__(self, conflict=False):
        self.data = {"parser": PARSER, "input": INPUT}
        self.puts = []
        self.conflict = conflict

    def get(self, url, *, max_bytes, timeout):
        return self.data["parser" if "parser" in url else "input"]

    def put(self, url, body, *, headers, timeout):
        self.puts.append((url, body, headers))
        if self.conflict:
            raise RuntimeError("output already exists")
        return 201


def env():
    return {
        "WORKER_JOB_ID": "job-1", "INPUT_URL": "https://host/input?sig=secret",
        "PARSER_URL": "https://host/parser?sig=secret", "OUTPUT_URL": "https://host/output?sig=secret",
        "RESULT_URL": "https://host/result?sig=secret", "INPUT_SHA256": hashlib.sha256(INPUT).hexdigest(),
        "INPUT_SIZE": str(len(INPUT)), "PARSER_SHA256": hashlib.sha256(PARSER).hexdigest(),
        "PARSER_SIZE": str(len(PARSER)),
        "WORKER_INPUT_NAME": "input.xlsx",
        "WORKER_OUTPUT_NAME": OUTPUT_NAME,
        "WORKER_OUTPUT_CONTENT_TYPE": OUTPUT_CONTENT_TYPE,
    }


def fake_run(**kwargs):
    return ({"contract_version": "synthetic-worker.v1", "job": kwargs["job"]}, 0, OUTPUT)


def test_happy_transfer_headers_and_manifest_are_safe():
    transport = FakeTransport()
    result, code = remote_job(env(), transport=transport, run=fake_run)
    assert code == 0 and result["status"] == "completed"
    assert result["input"]["verified"] and result["parser"]["verified"]
    assert all("secret" not in repr((body, headers)) for _, body, headers in transport.puts)
    output_headers = transport.puts[0][2]
    assert output_headers["x-ms-blob-type"] == "BlockBlob"
    assert output_headers["If-None-Match"] == "*"
    assert output_headers["Content-Type"] == OUTPUT_CONTENT_TYPE
    assert output_headers["Content-Length"] == str(len(OUTPUT))
    assert result["output"]["content_type"] == OUTPUT_CONTENT_TYPE


def test_size_or_hash_mismatch_fails_without_output():
    broken = env(); broken["INPUT_SIZE"] = "99"
    transport = FakeTransport()
    result, code = remote_job(broken, transport=transport, run=fake_run)
    assert code != 0 and result["status"] == "failed"
    assert len(transport.puts) == 1


def test_conflict_does_not_overwrite():
    result, code = remote_job(env(), transport=FakeTransport(conflict=True), run=fake_run)
    assert code != 0 and result["failure"] == "conflict"


def test_unknown_worker_environment_is_rejected_without_secret_leak():
    values = env(); values["WORKER_SECRET"] = "query-secret"
    transport = FakeTransport()
    try:
        remote_job(values, transport=transport, run=fake_run)
    except ValueError as exc:
        assert "query-secret" not in str(exc)
    else:
        raise AssertionError("unknown worker key was accepted")
    assert not transport.puts


def test_partial_remote_configuration_fails_closed(capsys):
    with patch.dict(os.environ, {"WORKER_JOB_ID": "partial-job"}, clear=True):
        assert main() == 2
    result = json.loads(capsys.readouterr().out)
    assert result == {
        "contract_version": "remote-worker.v1",
        "failure": "invalid_configuration",
        "job": "partial-job",
        "missing_names": sorted(key for key in __import__("worker.transfer", fromlist=["REQUIRED"]).REQUIRED if key != "WORKER_JOB_ID"),
        "status": "failed",
        "unknown_names": [],
    }