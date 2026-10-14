import json
import os
from worker.runtime import Limits, run_job


def parser(code):
    return code.encode()

GOOD = '''import json,sys
p=sys.argv[sys.argv.index("--output")+1]
json.dump({"schema":"agentquote.synthetic.v1","rows":[]},open(p,"w"),separators=(",",":"),sort_keys=True)
'''


def test_valid_and_secret_environment_absent():
    os.environ["AGENTQUOTE_TEST_SENTINEL"] = "must-not-reach-parser"
    code = '''import json,os,sys
if "AGENTQUOTE_TEST_SENTINEL" in os.environ or "IDENTITY_ENDPOINT" in os.environ or "IDENTITY_HEADER" in os.environ:
    raise SystemExit(23)
p=sys.argv[sys.argv.index("--output")+1]
json.dump({"schema":"agentquote.synthetic.v1","rows":[]},open(p,"w"),separators=(",",":"),sort_keys=True)
'''
    try:
        result, status = run_job(parser_bytes=parser(code), input_bytes=b"x")
        assert status == 0
        assert result["status"] == "completed"
    finally:
        os.environ.pop("AGENTQUOTE_TEST_SENTINEL", None)


def test_nonzero_and_invalid_output():
    result, code = run_job(parser_bytes=parser("raise SystemExit(7)"))
    assert code == 4 and result["failure"] == "parser_exit"
    result, code = run_job(parser_bytes=parser("open(sys.argv[-1], 'w').write('x')\nimport sys"))
    assert code in (4, 5)


def test_output_and_log_limits():
    huge = 'import sys; open(sys.argv[sys.argv.index("--output")+1],"w").write("x"*10000)'
    result, code = run_job(parser_bytes=parser(huge), limits=Limits(output_bytes=100))
    assert code == 3 and result["failure"] == "output_limit"
    noisy = 'import sys; print("x"*10000)'
    result, code = run_job(parser_bytes=parser(noisy), limits=Limits(log_bytes=100))
    assert code == 3 and result["failure"] == "log_limit"


def test_timeout_terminates_parser_process_group():
    code = 'import subprocess,sys,time; subprocess.Popen([sys.executable,"-c","import time; time.sleep(30)"]); time.sleep(30)'
    result, status = run_job(parser_bytes=parser(code), limits=Limits(timeout_seconds=.1))
    assert status == 3 and result["failure"] == "timeout"


def test_fixture_size_limit():
    result, code = run_job(limits=Limits(input_bytes=1))
    assert code == 2 and result["failure"] == "input_limit"
