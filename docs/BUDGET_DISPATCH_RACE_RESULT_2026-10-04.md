# Reservation eligibility race correction

Approved narrow local fix; branch fix/budget-dispatch-eligibility from aaff949.
Roadmap: QuickQuote Docs/PARSER_BUDGET_FINAL_REVIEW_2026-10-04.md.
Changed author_budget.py and two budget test files only; no live/paid actions.

Before: controller eligibility checks could become stale before reserve(), letting
a paused caller dispatch duplicate initial authoring or obsolete refinement with
a different failure hash after a competing completion.

Now: _inspect optionally captures validated claims/terminals while calculating
occupied slots. reserve uses that SAME snapshot for latest own-request completed
candidate, initial suppression, refinement predecessor equality and exact-intent
deduplication. Next immutable slot claim comes from that snapshot; conflicts hold
without rescan/advancement. Existing hold priority, group-wide slots, failure
consumption, unknown ownership rules, limits and legacy defaults unchanged.

Two parametrized controlled interleavings failed before the fix (returned started
instead of hold), pass afterward with zero caller dispatches. Existing standalone
tests updated to use valid predecessor histories, not invented source candidates.
Repeated prior/source candidates, mixed failures/author versions and split-group
exhaustion remain covered. No weakened checks or increased budget.

Verification:90focused passed;717full passed/1SDKskip59.75sec; diagnostics clean,
diff check clean. Independent reviewer GO for narrow local correction, inspected
during/after scan race behavior and shared-member accounting. Reviewer could not
independently run tests or compare Git tree; orchestrator commands provided suite
and diff evidence. Dedicated during-scan/after-snapshot/multi-member interleaving
tests remain useful follow-ups; reviewed statically, not new runtime proof.

Rollback: revert this focused commit, keep opt-in off, retain immutable records.
No UI files changed; mandatory UI suite not rerun for this producer-only unit.
No deployment/imagebuild/globalenablement/modelcall/import/CQ/grant changes.
Baseline semantic-role and rollout/canary gates remain. Independent GO is not
production readiness or automatic exception-only demo proof.