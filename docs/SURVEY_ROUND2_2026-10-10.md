# Survey-first round 2 - 2026-10-10

Branch `feature/survey-round2`, stacked on `fix/survey-round1` (006763d, live).
Approved by Nick from QuickQuote Docs/SURVEY_ROUND2_DESIGN_2026-10-10.md (D1-D4).
QuickQuote carries the request field, review page and panel changes.

Commits:
- 087394b: requested assemblies and per-requested-assembly choice.
- 951990d: review rows, obvious links, system acceptance v2.
- 6176170: banned parser calls stated to the author.

## Requested assemblies and the survey link

- Request field `requested_assemblies` (id, part number, revision), optional.
  - It needs `package_files`, is sorted by id, and has 1-50 entries.
  - It is part of `ProcessingRequest.fingerprint()` and of the choice identity
    only when present. Shared golden vector `d64f0b54...` with QuickQuote.
- Choice request and result v2. v1 artifacts are still read.
  - Survey view v2 shows the requested assemblies as refs R1..., never ids.
  - Reply v2 adds `requested_assembly: null | {ref, matched_core, evidence}`.
  - `SURVEY_INSTRUCTIONS_V2` adds the reading rules: customer-code prefix,
    build suffix, core number, sibling numbers, loose revisions, never link by
    elimination.
- `assembly_core.py` is one pure rule, used for both the survey link and the
  binding:
  - Strip letters joined to the first digit, and one final letters-only
    segment. Keep the full number if fewer than 2 groups or 5 digits remain.
  - Every core group must be equal, with any separators.
  - The match must not be followed by a numeric dash group (`-03`).
  - Revisions contradict only when their digit sequences clash. One may extend
    the other: V10 and V10.1 agree; v9 and v10 do not.
- BOMs group by confirmed link to a requested assembly for the primary choice.
- A BOM with no confirmed link is set aside as `not_mapped` and can be
  reopened. The warning is `SURVEY_BOM_NOT_MAPPED`, or
  `SURVEY_MAPPING_UNCONFIRMED` when the agent linked it but code could not
  confirm the link.
- Choice outcomes:
  - Copies confirmed for different assemblies: processed as
    `links_not_consistent`.
  - No BOM linked at all: the choice fails with `PACKAGE_NO_REQUESTED_BOM`.

## Review and automatic acceptance

- Review (v2 choice only):
  - `requested_assemblies` and `package_files` (one row per surveyed file) are
    added and covered by `manifest_sha256`.
  - `package_choice_findings` also passes `SURVEY_FILES_UNCLASSIFIED` and the
    two new warnings.
- `requested_links` marks a ready group with
  `evidence.requested_link {assembly_id, matched_text, reason}` only when all
  four conditions hold:
  1. Every source document of the group is confirmed for one requested
     assembly.
  2. The parsed part number names no other requested assembly. Unrelated
     text, such as a title, does not count.
  3. The parsed revision does not contradict the requested revision.
  4. No other ready group links to the same assembly.
- System acceptance policy/proof v2 (`requested_bindings`):
  - The caller's identities must be the plan's requested assemblies.
  - Each assembly is bound once, by exact identity or by its link, and the
    proof records the bindings.
  - Any package-choice finding still stops it.
  - v1 proofs stay valid; the API accepts both versions.

## Parser author

`BANNED_CALL_RULE` and `HTML_XLS_WORKBOOK_RULE` state what rule
`html_xls_banned_workbook_api` enforces. The places that now carry it:
- the canonical and HTML rules;
- the single-parser and bundle instructions;
- the attempt 2-6 guidance.

No AUTHORING_VERSION bump, as in round 1.

## Verification

- New tests: test_assembly_core.py (30), test_package_requested_assemblies.py
  (26), test_package_requested_review.py (9), plus author-rule assertions.
- Full suite: `py -3.12 -m pytest tests -q -p no:cacheprovider` 1342 passed.
- Not built or deployed.

## Round 3 (branch `feature/survey-round3`, after the A036 retest)

Approved by Nick from QuickQuote Docs/SURVEY_ROUND2_RETEST_A036_2026-10-11.md
(options A-D).

- **A. Link containment.** With requested assemblies, a refused citation in a
  file's provisional assembly or requested-assembly link drops only that part.
  - The link is confirmed from the file's other verified citations if possible,
    else it becomes unconfirmed with reason `citation_refused`.
  - The record keeps `refused_citations` and gets the warning
    `SURVEY_LINK_CITATION_REFUSED`.
  - Role evidence refusals and all v1 refusals are unchanged.
- **B/C. Prompt.**
  - Every citation quotes this file's own path or shown cells; a file name is
    path text, never a cell value.
  - The 011-0810-00 example is replaced.
  - Board (PCB) drawings, Gerber, drill and fabrication files belong to the
    assembly that uses the board.
- **D. Diagnosis.**
  - Refusals name their field.
  - A failed call records its exception class name.
  - A reply cut off at the output limit is reported as `SURVEY_REPLY_TRUNCATED`.

Verification: focused 102 plus 31 requested-assembly tests; the full suite has
1347 passing.
