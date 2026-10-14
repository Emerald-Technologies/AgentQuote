# Survey-first round 1 fixes - 2026-10-10

Branch `fix/survey-round1`, based on 3a31626 (live). Approved by Nick from
QuickQuote Docs/SURVEY_FIRST_FIX_BATCH_PROPOSAL_2026-10-10.md (items 2 and 4;
QuickQuote carries items 1 and 5; item 3 is the next session).

- Survey view paths and folder names (package_survey._PATH_SECRET): only links,
  e-mail addresses and key-like runs with no `-`, `_` or `/` are hidden. The
  shared `_SECRET` rule hid 17 of 75 A034 paths - exactly the 17 files refused
  on the first attempt - including identifiers such as 014-0810-U2. Cell values
  keep the shared rule.
- Parser author: the enforced broad-exception rule is now stated in
  CANONICAL_OUTPUT_RULES (EXCEPTION_POLICY_RULE), the single-parser
  SYSTEM_INSTRUCTION, the association bundle instructions and the guidance for
  budget attempts 2-6. The rule itself is unchanged. No AUTHORING_VERSION bump
  (acceptance criteria unchanged; a bump would re-author existing requests); the
  prompt text change is recorded in QuickQuote's risk register.

Verification: 102 focused tests (survey, authoring, foundry, budget, package
choice) and 1277 full tests passed
(`py -3.12 -m pytest tests -q -p no:cacheprovider`). Not built or deployed.
