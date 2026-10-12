"""Round 10 (A042, Nick 2026-10-12): parser stop feedback and the column-role wording.

- A parser stopped at the worker's time limit reports that limit (it leaves no traceback, so the
  feedback used to say only "parser failed" and every rewrite repeated the same slow reading).
- A column read only to decide item_category goes in ignored_columns (A041/A042 ENE: column 17
  "Item category" was neither cited nor declared, on every attempt).
Run: python -m pytest tests/test_parser_stop_feedback.py
"""
import hashlib

from bom_processor.candidate_authoring import CANONICAL_OUTPUT_RULES, ROW_MAP_RULE
from worker.runtime import Limits
from worker.transfer import _stop_reason, remote_job

SLOW = b"import time\nwhile True:\n    time.sleep(0.05)\n"
INPUT = b"PK\x03\x04synthetic"


class Transport:
    def __init__(self):
        self.puts = []

    def get(self, url, *, max_bytes, timeout):
        return SLOW if "parser" in url else INPUT

    def put(self, url, body, *, headers, timeout):
        self.puts.append((url, body))
        return 201


def env():
    return {"WORKER_JOB_ID": "job-1", "INPUT_URL": "https://host/input?sig=x", "PARSER_URL": "https://host/parser?sig=x",
            "OUTPUT_URL": "https://host/output?sig=x", "RESULT_URL": "https://host/result?sig=x",
            "INPUT_SHA256": hashlib.sha256(INPUT).hexdigest(), "INPUT_SIZE": str(len(INPUT)),
            "PARSER_SHA256": hashlib.sha256(SLOW).hexdigest(), "PARSER_SIZE": str(len(SLOW)),
            "WORKER_INPUT_NAME": "input.xlsx", "WORKER_OUTPUT_NAME": "output.xlsx",
            "WORKER_OUTPUT_CONTENT_TYPE": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}


def test_a_parser_stopped_at_the_time_limit_says_so():
    result, code = remote_job(env(), transport=Transport())
    assert code != 0 and result["status"] == "failed"
    message = result["failure_classification"]["error_message"]
    assert f"stopped at the {int(Limits().timeout_seconds)}-second time limit" in message
    assert "iter_rows(values_only=True)" in message


def test_stop_reasons_name_each_runtime_limit():
    assert "time limit" in _stop_reason({"failure": "timeout"})
    # Killed by the CPU limit: a negative exit code and no traceback.
    assert "time limit" in _stop_reason({"failure": "parser_exit", "exit_code": -24})
    assert _stop_reason({"failure": "parser_exit", "exit_code": 1, "stderr_tail": "ValueError: x"}) == ""
    assert "output workbook" in _stop_reason({"failure": "invalid_output"})


def test_rules_cover_speed_and_columns_read_only_for_item_category():
    rules = " ".join(CANONICAL_OUTPUT_RULES)
    assert "the parser must finish within a few seconds" in rules
    assert "only reads to decide item_category or which rows to keep" in ROW_MAP_RULE
