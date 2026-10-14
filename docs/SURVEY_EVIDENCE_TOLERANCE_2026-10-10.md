# Survey evidence tolerance and retry feedback - 2026-10-10

Branch `fix/survey-evidence-tolerance`, based on deployed f323766. Nick approved
edit/test/commit after QuickQuote's A031 diagnosis
(QuickQuote Docs/A031_SURVEY_FAILURE_DIAGNOSIS_2026-10-10.md): both survey
batches of the live A031 package were rejected for one citation each
(SURVEY_EVIDENCE_VALUE_WHITESPACE, SURVEY_EVIDENCE_NOT_IN_PATH), which retained
every BOM and skipped all primary-BOM choices.

## Changes

- `package_survey_check._evidence`: surrounding spaces are stripped instead of
  rejected; path and cell citations match as whole tokens ignoring letter case
  and the width of spaces, and the record keeps the text as written in the
  source. Partial tokens ('SSY-100' in 'ASSY-100'), joined words and absent text
  are still refused. The shared `_within_cell` used elsewhere is unchanged.
- `SurveyError` carries the survey file id (F1...) when a record fails;
  `verify_batch` attaches it.
- `package_survey_run`: the retry names the failing file id; the batch finding
  keeps `attempt_failures` (code and file id per attempt). No customer values.
- `choice_job.py`: the log line adds `survey_failures` (codes, file ids, counts).

Not changed: batch size, prompt, per-batch fail-closed rule, the rule that any
failed batch retains every BOM, parser-author code policy.

## Verification

- `py -3.12 -m pytest tests/test_package_survey.py tests/test_bom_package_choice.py -q`: 61 passed.
- `py -3.12 -m pytest tests -q -p no:cacheprovider --basetemp <QuickQuote>/.tmp_survey_tolerance_tests_full_20261010`:
  1271 passed in 104.49 s (7 new cases).

Not built, published or deployed. Live effect needs a reviewed image release of
the API/choice image and a deliberate A031 rerun.
