# Grouped validation failure feedback repair

Approved bounded local implementation; branch fix/grouped-validation-feedback,
base77320b0. Roadmap: QuickQuote Docs/AUTOMATIC_REPAIR_INVESTIGATION_2026-10-04.md.
No deployment, paid call, dependency or acceptance-role rule changes.

Changed candidate_validation.py, candidate_status.py and focused regression tests:
- Verify failed worker contract/job and pinned input/parser evidence before using
  classification. Failed results may lack output; successful checks unchanged.
- Distinguish fixed worker-wrapper parser-failure messages from infrastructure
  failures. Infrastructure/malformed/integrity cases hold, not paid refinement.
- Persist bounded sanitized per-fixture facts and one deterministic grouped
  failure event bound to aggregate evidence hash. Replay restores missing events.
- New-policy missing/unreadable event holds instead of legacy schema fallback.
- Feedback policy is evidence metadata, not validation-ID change; old evidence
  remains replay-compatible and immutable. Existing old failures are not migrated.
- Whole credential-like messages withheld, including bearer values, quoted values
  and quoted dictionary/JSON keys; paths/URLs redacted. Arbitrary source-like
  stderr and unmarked secrets cannot be exhaustively recognized: feedback remains
  untrusted model input, not semantic authority.

Verification: final51focused passed;627full passed/1SDKskip in55.87seconds;
editor diagnostics clean before final one-line regex correction; full tests/diff
checks pass afterward. Initial full test showed existing workbook byte-equivalence
fixture3vs4 mismatch; subsequent two full runs pass, exact flake cause unconfirmed.

Independent review found credential, replay/fallback and identity issues; corrected.
Second review found quoted-key bypass; corrected and final regressions/full suite
rerun. No final independent GO claimed; orchestrator validated narrow correction.

Residual: status trusts published event evidence-hash field without independently
recomputing it; producer storage authority remains existing boundary. Fixed-prefix
parser classification depends on current worker wrapper. Global budgets, ambiguous
starts, stale claims, exhaustion visibility, manual candidate lineage and semantic
role acceptance are unchanged and remain separate work. Old singleton best-effort
failure-event persistence unchanged. No claim automatic exception-only demo ready.

Rollback: revert this focused commit, preserve immutable old evidence. New evidence
adds policy metadata but retains old validation identity; existing persisted result
replays unchanged. Deploy only after explicit image/pin/canary approval.
Two unrelated dirty package-evidence docs preserved and excluded from commit.