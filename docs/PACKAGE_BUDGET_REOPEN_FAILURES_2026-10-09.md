# Package parser attempts and partial failures — 2026-10-09

Scope: QuickQuote plan `Docs/BOM_SELECTION_AND_LARGE_PACKAGE_PLAN_2026-10-07.md`,
section 8.8 step 6. Nick approved fixes 1A and 2A and work in both repositories.
Branch: `fix/package-budget-reopen-failures`, based on `a9bf48c`.
Built and verified offline; not deployed. No production settings, credentials,
model calls or customer data changed.

## Behavior

- New requests carrying `package_files` enroll new parser candidates in the
  existing six-slot accounting path. Previously persisted opt-out requests are
  never backfilled; other requests retain their existing config controls.
- A reopened BOM joins its customer/parser/RFQ/package-version/layout group.
  Existing claims consume the same slots; the seventh author or refinement
  attempt is held. There is no package-wide total and no 500-call cap.
- The first immutable policy pins the original package-choice request/result.
  Added sources must be explicitly reopened by that same stored, hash-verified
  choice and match the original document metadata. Each expanded membership
  binds its persisted parent plan. Unrelated additions and changed bytes fail
  closed. Policy, first membership and existing claims are never rewritten.
- A new member after any used slot inherits the first claimant's verified
  saved authoring mode. Missing/corrupt history or policy is never guessed.
- Choice calls retry once. After two failures, that assembly retains every
  BOM with `CHOICE_ASSEMBLY_FAILED`; other assembly choices continue. Generic
  caller exceptions are sanitized to `CHOICE_CALL_FAILED`.
- Survey batches retry once. Failed files explicitly have
  `classification_status=unclassified` (role remains the existing `other`),
  with a blocking `SURVEY_BATCH_FAILED` finding. Successful batches remain.
  Every request BOM is processed; primary-choice calls are skipped because
  survey failure prevents any exclusion. Transport details are not retained.
- Package review includes optional, hash-bound `package_choice_findings`
  naming document ids, file paths and known assemblies. Held candidate groups
  include `automation_error` and known `source_file_paths`/`source_assemblies`.
  Both AgentQuote and QuickQuote refuse automatic system acceptance of partial
  choice results. A person can use the existing package review path.

## Verification

`py -3.12 -m pytest tests -q`: **1244 passed in 259.21 seconds** on final code.
`py -3.12 -m pytest tests/test_package_budget_reopen_failures.py -q`:
**10 passed** before the final source-name assertions; the final full suite
includes those assertions.

New regressions cover used-count and saved-mode carry-over, idempotent rerun,
seventh-try refusal with named files/assemblies, tampered plan/result refusal,
unapproved additions, failed assembly choice alongside successful choice,
malformed/transport survey failures, successful batch preservation, and refusal
of system acceptance. Existing strict survey tests now assert the approved
unclassified fallback and per-stage call-limit hold.

QuickQuote UI verification and both commit ids are recorded in QuickQuote's
global `Docs/progress/progress.md`. Its risk register remains authoritative;
agent-raised items stay open for Nick's triage and live sign-off.

## Deployment and rollback

Deployment is plan step 7 and needs separate approval. Deploy QuickQuote first
so it understands optional review findings, then the matching AgentQuote API,
choice, candidate, validation and worker images containing the shared changes.
Keep the existing step 4 remote-worker.v3 and step 5 evidence-v6 coordination.
New source extensions require this budget reader in API and author workers.

Before live enrollment, reverting this commit rolls back the change. After
expanded memberships have been written, older readers hold them with
`budget_invalid`; use compatible images or a new package version. Do not edit
or delete immutable policies, memberships or claims to reset attempts.

Next: review the full deployment diff and rerun A030 after approved rollout.
Watch live reopen count carry-over and partial survey/choice findings. No live
proof is claimed by the offline results.
