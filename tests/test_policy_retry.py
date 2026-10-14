"""Round 12 (A044, Nick 2026-10-12): a refused rewrite neither blocks progress nor bans ordinary tools.

- The code policy bans the built-in eval/exec/compile, not module functions with those names (re.compile).
- A fix attempt that died before producing a parser (refused code, failed model call) may be retried for
  the same failure, counted against the six-attempt budget, and the retry is told the code rules.
Run: python -m pytest tests/test_policy_retry.py
"""
import pytest

from bom_processor.candidate_authoring import POLICY_RETRY_RULE, AuthoringError, _validate_code
from bom_processor.candidate_automation import CandidateAutomationService
from test_author_budget_integration import NOW, Starter, finish, planned

HEADER = ("import argparse\nparser = argparse.ArgumentParser()\nparser.add_argument('--input', required=True)\n"
          "parser.add_argument('--output', required=True)\nparser.add_argument('--row-map')\nargs = parser.parse_args()\n")


def test_re_compile_is_allowed_and_the_built_ins_are_still_refused():
    _validate_code(HEADER + "import re\nPATTERN = re.compile(r'[A-Z]+')\n")
    for code in ("compile('1', 'x', 'eval')\n", "eval('1')\n", "exec('x = 1')\n", "import pandas\npandas.read_html('x')\n"):
        with pytest.raises(AuthoringError) as error:
            _validate_code(HEADER + code)
        assert error.value.rule_id == "html_xls_banned_workbook_api"


def test_a_fix_attempt_that_died_without_a_parser_is_retried_and_told_the_code_rules():
    storage, _, request_id, _ = planned()
    starter = Starter()
    automation = CandidateAutomationService(storage, starter, author_budget_enabled=True, budget_clock=lambda: NOW)
    automation.start_author(request_id)
    finish(storage, starter.calls[-1], "completed", "candidate-A")
    event = {"failure_message": "row_accounting_failed rows=1"}
    assert automation.start_author_refinement(request_id, "candidate-A", event) == "started"
    assert "AUTHOR_POLICY_RETRY" not in starter.calls[-1]
    finish(storage, starter.calls[-1], "failed")  # the rewrite was refused before producing a parser
    assert automation.start_author_refinement(request_id, "candidate-A", event) == "started"
    assert starter.calls[-1]["AUTHOR_POLICY_RETRY"] == "true"
    assert len(starter.calls) == 3


def test_a_fix_that_produced_a_parser_is_still_never_repeated():
    storage, _, request_id, _ = planned()
    starter = Starter()
    automation = CandidateAutomationService(storage, starter, author_budget_enabled=True, budget_clock=lambda: NOW)
    automation.start_author(request_id)
    finish(storage, starter.calls[-1], "completed", "candidate-A")
    event = {"failure_message": "bad header"}
    automation.start_author_refinement(request_id, "candidate-A", event)
    finish(storage, starter.calls[-1], "completed", "candidate-B")
    assert automation.start_author_refinement(request_id, "candidate-A", event) == "budget_no_progress"


def test_the_retry_rule_names_the_import_and_call_rules():
    assert "never call the built-in eval, exec or compile" in POLICY_RETRY_RULE
    assert "subprocess" in POLICY_RETRY_RULE and "broad except" in POLICY_RETRY_RULE
