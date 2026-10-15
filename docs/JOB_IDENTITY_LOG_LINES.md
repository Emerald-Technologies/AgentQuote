# Job identity log lines (`agentquote-job-line.v1`)

QuickQuote #121 (related #56). During a dry run, job logs carried only `request_id`, `candidate_id`
and a file hash, so an operator had to guess which quote and BOM a job belonged to. Every author,
author-refinement, validator, parser-worker, choice and evidence job now logs exactly one START line
and one END line that name the work.

Code: `bom_processor/job_identity_log.py` (pure line builders, read-only loaders, `JobLog` emitter).
Tests: `tests/test_job_identity_log.py`.

## The line

One line of JSON on stdout (`json.dumps(..., sort_keys=True)`), like the jobs' other lines. Every
field is always present; an unknown value is `null`, never a guess. Field names are a contract with
QuickQuote and do not change; a change needs a new `schema` value.

| Field | Value |
|---|---|
| `schema` | `"agentquote-job-line.v1"` |
| `event` | `"agentquote_job"` |
| `phase` | `"start"` or `"end"` |
| `job` | `author`, `author_refinement`, `validator`, `parser_worker`, `choice` or `evidence` |
| `at` | UTC time, `YYYY-MM-DDTHH:MM:SSZ` |
| `request_id` | The job's `REQUEST_ID` |
| `rfq_uuid` | QuickQuote RFQ uuid (lower case) |
| `package_version` | Package version as a string (`"1"`) |
| `package_plan_id` | AgentQuote package plan id, when the request belongs to one |
| `assembly_id` | Requested assembly uuid, when the work is bound to exactly one |
| `assembly` | That assembly's part number as QuickQuote sent it |
| `source_file` | Source file NAME only (never a path or URL) |
| `candidate_id` | Parser candidate id, when known |
| `attempt` | Attempt number, when the job knows it |
| `max_attempts` | Attempt limit, when the job knows it |
| `result` | `null` on START; `succeeded`, `failed` or `skipped` on END |
| `reason_code` | `null` or the job's existing, most specific stable code |
| `reason` | `null` or one plain sentence from a fixed table keyed by `reason_code`; an unknown code reads "The job stopped with an unrecognised error." |
| `worker_job_id` | **`parser_worker` lines only**: the worker job id (`validate-...`) the validator dispatched, to pair START and END |

`result` says whether the job reached its own goal: the author wrote a parser; the validator
validated the candidate (a finished validation that leaves the candidate `generated_unvalidated` is
`failed` with that status as `reason_code`); the parser worker's result was verified as completed
(judging its output belongs to the validator); the choice or evidence run completed. `skipped`
(`JOB_NOT_RUN`) means a choice or evidence run found the request already finished or owned by
another run.

## Ordering

- START is printed once the job has read its identity, just before its work begins. If the job stops
  before that point (bad environment, missing settings), START is printed with what is known,
  immediately before END.
- END is printed immediately **before** the job's existing final result line, so that line stays the
  last line. Existing lines are unchanged.
- The parser worker runs sandboxed and its stdout must stay exactly one result line, so it prints
  nothing new and gets no new settings. The validator, which starts each worker job and knows its
  identity, prints `job: "parser_worker"` START/END lines around each worker job it dispatches,
  between its own START and END.

## Where each field comes from

All reads are read-only and use records the job already reads. If any read fails the fields stay
`null` and the job's outcome, exit code and other output are unchanged.

| Field | author / author_refinement, validator, parser_worker | choice | evidence |
|---|---|---|---|
| `request_id` | `REQUEST_ID` | `REQUEST_ID` | `REQUEST_ID` |
| `rfq_uuid`, `package_version` | Must agree across: the bound package-choice request, the author-budget plan identity, and the sources' QuickQuote namespace (`<customer>/rfq-<uuid>/package-<n>/original-boms/`); otherwise `null` | Package-choice request | Request `context.qqUuid` / `context.packageVersion` |
| `package_plan_id` | Author-budget membership's `plan_id`; `null` when the request is not budget-enrolled (the plan links to requests, not back) | `null` (the choice precedes the plan) | `context.bomPlanId` |
| `assembly_id`, `assembly` | Bound package-choice result: every source document confirmed for the same one requested assembly; name from that request's `requested_assemblies` | `null` (whole package) | When `context.assemblies` has exactly one |
| `source_file` | Package file `file_name` matched by hash, else the staged BOM file name; `null` when sources name different files. parser_worker: the fixture it ran | `null` | `null` |
| `candidate_id` | validator and parser_worker: `CANDIDATE_ID`; author: on END, the candidate it wrote | `null` | `null` |
| `attempt`, `max_attempts` | Author: `AUTHOR_ATTEMPT` and the automation's fixed initial limit (`CandidateAutomationService.MAX_ATTEMPTS`). Refinement: `AUTHOR_ATTEMPT`; limit `null` because it is configurable in the API process (`BOM_CANDIDATE_MAX_AUTHOR_ATTEMPTS`) and unknown to the job. Budgeted author: the group's budget slot and `author_budget.LIMIT`. Validator and parser_worker: `null` | `null` | Request attempt (initial 1, retry or successor attempt) and the retry/successor limit; limit `null` for v1 requests |

The author job kind comes from `AUTHOR_ATTEMPT_KIND`, else the budget claim's phase, else
`author_refinement` when refinement ids are set, else `author`.

## Never logged

Cell values or any other file content; exception messages (they can carry package data: only
existing stable codes or exception class names are used); paths, blob URLs or SAS URLs; hashes beyond
ids already logged; secrets, tokens or environment values. Every field is shape-checked: an id must
be a plain identifier, a uuid must be a uuid, a file name may not contain `/`, `\`, `:`, `?`, `#` or
`%`, and anything else becomes `null`. The assembly part number and the file name are logged because
QuickQuote already shows them.

## How QuickQuote uses it

QuickQuote's `qq status --job-log <file>` reads a saved job log, keeps lines whose `schema` is
`agentquote-job-line.v1`, pairs START and END per job run (by `job`, `request_id`, `candidate_id`,
`attempt` and, for parser workers, `worker_job_id`), and joins them per quote (`rfq_uuid`) and per BOM
(`assembly_id` / `source_file`) to show which jobs ran, which are still running and why one failed.
