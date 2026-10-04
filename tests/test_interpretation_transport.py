"""Opt-in sidecar transport tests; synthetic bytes and fake storage only."""
import base64
import hashlib
import json
from datetime import datetime, timezone

import pytest

from bom_processor.job_orchestrator import WorkItem, SasUrls, AzureJobStarter
from worker.runtime import run_job, Limits
from worker.transfer import remote_job
from test_interpretation_coverage import fixture
from test_worker_transfer import env, FakeTransport


def parser_bytes(output, sidecar):
    return ("import argparse,base64\n"
            "p=argparse.ArgumentParser();p.add_argument('--input');p.add_argument('--output');"
            "p.add_argument('--interpretation');a=p.parse_args()\n"
            f"open(a.output,'wb').write(base64.b64decode('{base64.b64encode(output).decode()}'))\n"
            f"open(a.interpretation,'wb').write(base64.b64decode('{base64.b64encode(sidecar).decode()}'))\n").encode()


def test_real_offline_parser_sidecar_is_verified_and_hashed():
    source, output, proposal = fixture()
    sidecar = json.dumps(proposal).encode()
    ran = run_job(parser_bytes=parser_bytes(output, sidecar), input_bytes=source,
                  input_name="input.xlsx", output_name="output.xlsx", output_policy="opaque",
                  return_output=True, interpretation_name="interpretation.json",
                  limits=Limits(timeout_seconds=10))
    assert ran[1] == 0 and ran[2:] == (output, sidecar)
    assert ran[0]["interpretation"]["sha256"] == hashlib.sha256(sidecar).hexdigest()
    assert ran[0]["interpretation"]["verification"]["automatic_acceptance_authorized"] is False


def test_missing_or_tampered_sidecar_fails_before_success():
    source, output, proposal = fixture()
    proposal["source"]["sha256"] = "0" * 64
    ran = run_job(parser_bytes=parser_bytes(output, json.dumps(proposal).encode()), input_bytes=source,
                  input_name="input.xlsx", output_name="output.xlsx", output_policy="opaque",
                  return_output=True, interpretation_name="interpretation.json", limits=Limits(timeout_seconds=10))
    assert ran[1] == 5 and ran[0]["failure"] == "interpretation_verification_failed"


def test_opt_in_transfer_publishes_sidecar_before_completed_result():
    source, output, proposal = fixture(); sidecar = json.dumps(proposal).encode()
    transport = FakeTransport(); transport.data["input"] = source
    settings = env(); settings.update(INPUT_SHA256=hashlib.sha256(source).hexdigest(), INPUT_SIZE=str(len(source)),
                                      WORKER_INTERPRETATION_NAME="interpretation.json",
                                      INTERPRETATION_URL="https://host/interpretation?sig=secret")
    def run(**kwargs):
        assert kwargs["interpretation_name"] == "interpretation.json"
        return {"status": "completed"}, 0, output, sidecar
    result, code = remote_job(settings, transport=transport, run=run)
    assert code == 0 and result["contract_version"] == "remote-worker.v2"
    assert len(transport.puts) == 3
    assert transport.puts[1][1] == sidecar
    assert all(headers["If-None-Match"] == "*" for _, _, headers in transport.puts)
    assert "secret" not in json.dumps(result)
    assert result["interpretation"]["verification"]["semantic_acceptance"] == "not_evaluated"


def test_partial_opt_in_configuration_rejected():
    settings = env(); settings["WORKER_INTERPRETATION_NAME"] = "interpretation.json"
    with pytest.raises(ValueError, match="incomplete interpretation"):
        remote_job(settings, transport=FakeTransport())


def test_sidecar_name_cannot_escape_or_alias_output():
    with pytest.raises(ValueError, match="invalid interpretation_name"):
        WorkItem("job", "boms", "parsers", "parsers", "source", "parser", "a"*64, 1,
                 "b"*64, 1, "attempt/job", interpretation_name="../output.xlsx")
    with pytest.raises(ValueError, match="destination collision"):
        WorkItem("job", "boms", "parsers", "parsers", "source", "parser", "a"*64, 1,
                 "b"*64, 1, "attempt/job", result_name="interpretation.json",
                 interpretation_name="interpretation.json")


@pytest.mark.parametrize("keys", [{"INTERPRETATION_URL": "https://host/sidecar?sig=x"},
                                 {"WORKER_INTERPRETATION_NAME": "interpretation.json"}])
def test_sidecar_only_cli_intent_never_runs_synthetic(monkeypatch, capsys, keys):
    import os
    from worker.__main__ import main
    for key in list(os.environ):
        if key.startswith("WORKER_") or key in env() or key == "INTERPRETATION_URL":
            monkeypatch.delenv(key, raising=False)
    for key, value in keys.items():
        monkeypatch.setenv(key, value)
    assert main() == 2
    assert json.loads(capsys.readouterr().out)["failure"] == "invalid_configuration"


def test_invalid_html_returns_structured_interpretation_failure():
    source, output, proposal = fixture()
    ran = run_job(parser_bytes=parser_bytes(output, json.dumps(proposal).encode()), input_bytes=b"not html",
                  input_name="input.xls", output_name="output.xlsx", output_policy="opaque",
                  return_output=True, interpretation_name="interpretation.json", limits=Limits(timeout_seconds=10))
    assert ran[1] == 5 and ran[0]["failure"] == "interpretation_verification_failed"


def test_starter_passes_upload_url_only_to_worker_configuration():
    item = WorkItem("job", "boms", "parsers", "parsers", "source", "parser", "a"*64, 1,
                    "b"*64, 1, "attempt/job", interpretation_name="interpretation.json")
    assert item.interpretation_blob == "attempt/job/interpretation.json"
    urls = SasUrls("https://host/input?sig=x", "https://host/parser?sig=x", "https://host/output?sig=x",
                   "https://host/result?sig=x", datetime.now(timezone.utc), "https://host/interpretation?sig=x")
    assert "sig=" not in repr(urls)
    class Credential:
        def get_token(self, scope):
            class Token: token = "not-logged"
            return Token()
    captured = []
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self): return b'{"name":"execution"}'
    def opener(request): captured.append(json.loads(request.data)); return Response()
    starter = AzureJobStarter(subscription_id="subscription", resource_group="group", job_name="worker",
                              credential=Credential(), opener=opener)
    assert starter.start(item, urls) == "execution"
    values = {entry["name"]: entry["value"] for entry in captured[0]["containers"][0]["env"]}
    assert values["WORKER_INTERPRETATION_NAME"] == "interpretation.json"
    assert values["INTERPRETATION_URL"] == urls.interpretation_url