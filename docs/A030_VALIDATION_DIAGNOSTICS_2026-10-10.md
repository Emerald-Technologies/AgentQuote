# A030 validation diagnostics - 2026-10-10

Roadmap: QuickQuote Docs/BOM_SELECTION_AND_LARGE_PACKAGE_PLAN_2026-10-07.md
section 8.8 step 7. Diagnosis: QuickQuote
Docs/A030_FAILURE_DIAGNOSIS_2026-10-10.md. Branch:
fix/a030-validation-diagnostics, based on deployed source 4455340.

Nick approved this source-only inspection/edit/test/commit task after read-only
diagnosis. That approval excludes deployment, new RFQ/package, live data changes
and additional paid model calls. The QuickQuote global progress/risk register
remains authoritative. Unrelated scratch files and previous commits are preserved.

## Problem and correction

A030's first survey batch was rejected with SURVEY_EVIDENCE_INVALID and the
approved safe fallback retained all four spreadsheets. All four subsequent
author jobs failed with MODEL_FAILED at document_role/value_error. The role
exception wrapper discarded existing safe rejection codes; the rejected proposal
was not persisted. The precise live role rule remains unknown, even after this
source fix, because it cannot add diagnostics to past executions.

author_failure.py now retains rule_id only for a DocumentRoleError raised at the
document_role boundary with exactly one argument matching one of five known
validator codes. Generic exceptions, unknown values, malformed arguments and
codes from another boundary remain sanitized. candidate_authoring.py propagates
that rule through the existing AuthoringError rule_id field. The existing job
logger/attempt marker can report it without a new API field or stored raw reply.
MODEL_FAILED and the existing stage/category fields remain intact.

package_survey_check.py distinguishes invalid evidence record, source, fields,
value type, empty value, over-256-character value and surrounding whitespace.
Existing cell-locator/path/source-value checks remain. The same rejected-reply
retry passes only the stable code; after two failures, the existing blocking
SURVEY_BATCH_FAILED finding stores it in the existing failure_code field.
No system prompts, accepted evidence, retry limit, budget accounting,
business decision, automatic acceptance or storage location changed.

## Verification

- Focused suite: 89 passed in 5.81 seconds, including role validation, author/job
  diagnostics, survey fallback and reopen/six-attempt regressions.
- Full suite: 1264 passed in 144.79 seconds (20 new cases; zero failures).
- Twenty new cases: five real role-validator/service failures, five malicious
  or wrong-boundary rule refusals, nine malformed survey replies with exactly
  two calls and no response-content leakage, and one additional existing-job
  reporter case for the optional rule_id field.
- Initial focused collection found a missing pytest import in the new job-test
  parameterization; corrected the test import, then all focused tests passed.
- Existing pytest cache has a read-permission issue and is preserved. Runs use
  -p no:cacheprovider plus unique temporary directories in the QuickQuote
  workspace. No dependency installation/version change.
- Source diff whitespace and added secret-pattern checks pass. Full-suite and
  final staged checks passed before commit.

Commands run from AgentQuote:

    py -3.12 -m pytest tests/test_author_failure.py tests/test_document_role.py tests/test_candidate_job.py tests/test_package_survey.py tests/test_package_budget_reopen_failures.py -q -p no:cacheprovider --basetemp C:/Users/Nicholas.Buck/Documents/GitHub/QuickQuote/.tmp_a030_diagnostic_tests_focused_v2_20261010
    py -3.12 -m pytest tests -q -p no:cacheprovider --basetemp C:/Users/Nicholas.Buck/Documents/GitHub/QuickQuote/.tmp_a030_diagnostic_tests_full_20261010

## Review, deployment and rollback

This corrects the confirmed diagnostic loss. It does not prove a fix for the
unrecovered role proposal or successful A030 primary selection. Future validator
codes require explicit allowlist updates/tests before being exposed as rule_id.

Blast radius: author failure reporting and survey rejection codes for all callers.
The more specific survey code also improves existing corrective feedback.
Rollback: revert the focused source commit or retain the current pinned live
images. No artifact/claim deletion, migration or budget reset is required.

Next: review source/tests, then prepare a separately approved image-build/rollout
and controlled test proposal. Current A030 package replays its immutable
completed survey result; a new test needs fresh RFQ/package identity or an
approved safe retry design. Current package intake and both CQ continuations
remain off. Preserve A030 results, claims and history. No new live acceptance
or automatic risk closure is claimed by offline verification.
