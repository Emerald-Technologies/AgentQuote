"""Agent primary-BOM proposal cross-check (synthetic, offline)."""
import copy

import pytest

from bom_processor.bom_primary_selection import (SELECTION_GUIDANCE, VERSION, PrimarySelectionError,
                                                 cross_check)
from test_bom_selection import MOD, PLAIN, line

A, B, C = "a" * 64, "b" * 64, "c" * 64


def proposal(primary, others):
    files = [{"fixture_sha256": primary, "disposition": "primary", "rationale": "Most complete."}]
    files += [{"fixture_sha256": key, "disposition": value, "rationale": "Agent reason."}
              for key, value in others.items()]
    return {"version": VERSION, "primary_fixture_sha256": primary, "files": files,
            "rationale": "Agent chose the BOM with procurement data and a parent row.", "findings": []}


def test_agreement_allows_selection_and_records_every_file():
    result = cross_check(proposal(A, {B: "redundant"}), {A: MOD, B: PLAIN})
    assert result["agreement"] == "agreed" and result["automatic_selection_allowed"] is True
    assert result["dispositions"] == {A: "primary", B: "redundant"}
    assert result["cross_checks"][0]["alignment"]["relation"] == "superset"


def test_agent_choosing_the_contained_bom_is_a_disagreement():
    result = cross_check(proposal(B, {A: "redundant"}), {A: MOD, B: PLAIN})
    assert result["agreement"] == "needs_review" and result["review_reasons"] == ["disagrees:redundant"]
    result = cross_check(proposal(B, {A: "obsolete"}), {A: MOD, B: PLAIN})
    assert result["review_reasons"] == ["disagrees:obsolete"]


def test_judgments_the_data_cannot_confirm_go_to_review_with_both_views():
    other = PLAIN + [line("1000-0050", None, 1, "BSS138")]
    result = cross_check(proposal(A, {B: "obsolete"}), {A: MOD, B: other})
    assert result["review_reasons"] == ["not_confirmable:obsolete"]
    assert result["cross_checks"][0]["alignment"]["right_only_lines"] == 1
    assert result["proposal"]["files"][1]["disposition"] == "obsolete"


def test_conflicts_and_blocking_findings_require_review():
    conflicting = [dict(row) for row in PLAIN]
    conflicting[2]["quantity"] = 9
    assert cross_check(proposal(A, {B: "redundant"}), {A: MOD, B: conflicting})["review_reasons"] == [
        "conflict:redundant"]
    blocked = proposal(A, {B: "redundant"})
    blocked["findings"] = [{"code": "UNSURE", "severity": "blocking", "rationale": "Two current revisions."}]
    assert cross_check(blocked, {A: MOD, B: PLAIN})["review_reasons"] == ["blocking_finding"]


def test_single_bom_needs_no_comparison():
    result = cross_check(proposal(A, {}), {A: MOD})
    assert result["agreement"] == "agreed" and result["cross_checks"] == []


@pytest.mark.parametrize("mutate, code", [
    (lambda p: p["files"].pop(), "PRIMARY_SELECTION_COVERAGE_INVALID"),
    (lambda p: p["files"][1].update(disposition="primary"), "PRIMARY_SELECTION_PRIMARY_INVALID"),
    (lambda p: p.update(primary_fixture_sha256=C), "PRIMARY_SELECTION_PRIMARY_INVALID"),
    (lambda p: p["files"][1].update(disposition="better"), "PRIMARY_SELECTION_FILE_INVALID"),
    (lambda p: p["files"].append(copy.deepcopy(p["files"][1])), "PRIMARY_SELECTION_FILE_INVALID"),
    (lambda p: p.update(extra=1), "PRIMARY_SELECTION_INVALID"),
])
def test_proposal_shape_is_strict(mutate, code):
    value = proposal(A, {B: "redundant"})
    mutate(value)
    with pytest.raises(PrimarySelectionError, match=code):
        cross_check(value, {A: MOD, B: PLAIN})


def test_guidance_leaves_the_choice_to_judgment():
    text = SELECTION_GUIDANCE.casefold()
    for phrase in ("primary", "redundant", "inferior", "obsolete", "warning finding"):
        assert phrase in text
