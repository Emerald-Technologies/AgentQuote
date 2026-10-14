# Opt-in parser group budget: local verification

Approved design: QuickQuote Docs/PARSER_GROUP_IDENTITY_FINDINGS_2026-10-04.md.
Branch feature/parser-group-budget, baseb7233c7. No deployment/paidcalls/grants.
Existing unrelated docs/PACKAGE_EVIDENCE_JOBS.md and MODEL_ADAPTER.md preserved.

Six shared initial/refinement reservations bind to trusted quote/package/profile
groups; source order normalized only for new opt-in requests. Immutable required
intent precedes request publication; incomplete enrollment holds. Existing opt-out
requests stay legacy. One randomized reservation owner before dispatch; durable
dispatch intent before management I/O, unknown acknowledgment holds even after
worker terminal publication. Worker acquires single-use execution ownership before
model creation; terminals require matching owner. All fixed-slot facts validated;
orphan/invalid records hold. Exact refinement intents deduplicated; stale successor
replays do not dispatch. Exhausted/in-flight/stalled/unknown/no-progress/invalid
codes projected to client. Read-only package status uses budget lineage without
enabling dispatch. Legacy defaults unchanged; flag remains default-off.

Verification:93focused budget/entrypoint tests;715fullpassed/1SDKskip58.22sec.
UI typecheck clean/lint0errors3oldwarnings/1619passed8skipped188.28sec. UI suite
ran before final producer ownership tightening; full producer suite rerun after.
Diff checks clean. Independent review twice NO-GO identified enrollment/ownership/
replay/lineage/unknown and stricter terminal/orphan/legacy boundaries; all addressed
by orchestrator with regressions/full suite. No independent finalGO claimed.

Limits: controller reservations not provider/invoice limits; privileged direct
starts outside accounting. Thirty-minute stall is a hold, not cancellation/recovery.
Fifty source test is coverage/order accounting, not fifty-assembly capacity proof.
Source-role semantic gate still incomplete. Fresh enrollment and no-progress
workflow need live canary/independent final review before enablement. No automatic
policy migration for A021 manual history, refunds, claim stealing or deletion.
Policy is Blob-backed producer evidence; primary application state remains SQL.

Rollback: keep opt-in disabled/revert focused changes; retain immutable records.
No deletion to reset budget. Coherent image rollout/canary separate approval.