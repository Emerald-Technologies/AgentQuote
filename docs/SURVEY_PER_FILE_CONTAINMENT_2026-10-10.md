# Survey per-file containment - 2026-10-10

Branch `fix/survey-per-file-containment`, based on 6421a18 (live). Nick chose
per-file containment plus recorded refusals after QuickQuote's A032 retest:
both survey batches were again discarded for one refused path citation each,
on a PDF, a Gerber file and a text file (QuickQuote
Docs/SURVEY_TOLERANCE_RELEASE_AND_RETEST_PLAN_2026-10-10.md, Result).

## Behavior

- `verify_batch_files` keeps every record that passes; a malformed reply or
  wrong coverage still refuses the whole reply. `verify_batch` keeps its old
  all-or-nothing contract for existing callers.
- The retry asks again only about refused files (`sub_batch`), naming each
  survey id and code; a whole-reply refusal still retries the whole batch.
- A file still refused after the retry is unclassified:
  - spreadsheet kinds: `SURVEY_BATCH_FAILED`/`SURVEY_CALL_LIMIT`, blocking, so
    package choice retains every BOM (unchanged fail-closed rule);
  - any other kind: `SURVEY_FILES_UNCLASSIFIED`, warning, does not stop the
    primary-BOM choice.
- Findings keep `attempt_failures` (codes, ids) and `rejected_citations` (the
  refused source/value/cell) in the stored result only. The choice job log line
  lists codes, severities and survey ids, never values.
- The QuickQuote review contract is unchanged: `package_resolution` still
  projects only the three existing codes, so the new warning is not shown on the
  review page (Nick chose log and stored result; review display is a follow-up).

## Verification

- `py -3.12 -m pytest tests/test_package_survey.py tests/test_bom_package_choice.py -q -p no:cacheprovider`: 65 passed.
- `py -3.12 -m pytest tests -q -p no:cacheprovider --basetemp <QuickQuote>/.tmp_survey_containment_tests_full_20261010`:
  1275 passed in 106.67 s.

Not built, published or deployed.
