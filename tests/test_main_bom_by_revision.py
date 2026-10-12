"""Round 13 (A044, Nick 2026-10-12): main-BOM choice by revision evidence and the review preview route.

- The model's pick is confirmed when it is the only candidate whose own evidence carries a distinctive
  requested revision (A044: "MOD BOM ... REV V10p1-B" for a request of V10p1-B).
- POST .../review returns the review with a person's decisions applied, read-only, for approval binding.
Run: python -m pytest tests/test_main_bom_by_revision.py
"""
import json

from bom_processor.bom_candidate_choice import confirmed_by_revision
from deterministic_api import PackageApi
from test_package_resolution import api_config, auth_headers


def choice(primary="F1", paths=None, values=None):
    paths = paths or {"F1": "pkg/ENE/MOD BOM_Yondu_ENE010-0810-U2-TK REV V10p1-B (4-16-26).xlsx",
                      "F2": "pkg/ENE/010-0810-U2_yondu_v1p1_4-16-2026.xlsx"}
    values = values or {}
    return {"primary": primary, "files": [{"file_id": file_id, "path": path,
                                          "evidence": [{"source": "cell", "value": values.get(file_id, "Quantity")}]}
                                         for file_id, path in paths.items()]}


def test_the_only_candidate_carrying_the_requested_revision_confirms_the_pick():
    assert confirmed_by_revision(choice(), "V10p1-B") is True


def test_no_confirmation_when_the_pick_lacks_it_or_another_candidate_carries_it_too():
    assert confirmed_by_revision(choice(primary="F2"), "V10p1-B") is False
    assert confirmed_by_revision(choice(values={"F2": "Rev V10P1-B"}), "V10p1-B") is False


def test_short_or_plain_revisions_never_decide_on_their_own():
    for revision in ("13", "A", "B2", "", None):
        assert confirmed_by_revision(choice(paths={"F1": "ASSY rev 13 B2 A.xlsx", "F2": "old.xlsx"}), revision) is False


def test_the_review_preview_route_applies_decisions_and_writes_nothing():
    class Service:
        def __init__(self):
            self.calls = []

        def review(self, plan_id, assembly_mappings=None, conflict_dispositions=None):
            self.calls.append((plan_id, assembly_mappings, conflict_dispositions))
            return {"version": "package-review.v1", "conflict_dispositions": conflict_dispositions}

        def approve(self, **value):
            raise AssertionError("a preview never approves")

    service = Service()
    api = PackageApi(api_config(), lambda: service)
    body = json.dumps({"conflict_dispositions": {"g" * 64: "f" * 64}}).encode()
    status, response = api.handle_post(auth_headers(), "plan-1", "review", body)
    assert status == 200 and response["conflict_dispositions"] == {"g" * 64: "f" * 64}
    assert service.calls == [("plan-1", None, {"g" * 64: "f" * 64})]
    status, response = api.handle_post(auth_headers(), "plan-1", "review",
                                       json.dumps({"reason": "not a review field"}).encode())
    assert status == 422
