"""Offline old/new budget interoperability against the deployed historical source.

Inputs: Git-pinned old module and synthetic recovery records. Output: fail-closed
old runtime and successful new ownership. Run pytest on this file; no network.
"""
import subprocess
import types
from pathlib import Path

import pytest

from bom_processor.author_budget import begin_execution, complete
from bom_processor.author_recovery import reserve_recovery
from test_author_recovery import failed_refinement

OLD_COMMIT = "ad03cd9"


def test_old_runtime_rejects_recovery_before_execution_or_write():
    source = subprocess.check_output(
        ["git", "show", f"{OLD_COMMIT}:bom_processor/author_budget.py"],
        cwd=Path(__file__).resolve().parents[1], text=True)
    old = types.ModuleType("bom_processor._historical_budget_test")
    old.__package__ = "bom_processor"
    exec(compile(source, f"{OLD_COMMIT}:author_budget.py", "exec"), old.__dict__)
    storage, budget, authority = failed_refinement()
    claim = reserve_recovery(budget, authority)
    assert claim["status"] == "reserved"
    before = dict(storage.registry)
    controller = old.Budget(storage, budget.request_id, clock=budget.clock)
    assert controller.inspect()["reason"] == "budget_invalid"
    assert controller.reserve("author-refinement", authority["source_candidate"],
                              authority["failure_hash"])["reason"] == "budget_invalid"
    arguments = (storage, claim["group_id"], claim["slot"],
                 budget.request_id, claim["reservation_hash"])
    with pytest.raises(old.BudgetError):
        old.begin_execution(*arguments)
    assert storage.registry == before
    budget.mark_started(claim, "offline-recovery")
    owner = begin_execution(*arguments)
    complete(*arguments, "completed", "replacement", execution_owner=owner)
    assert budget.selected_candidate() == "replacement"
    assert controller.inspect()["reason"] == "budget_invalid"