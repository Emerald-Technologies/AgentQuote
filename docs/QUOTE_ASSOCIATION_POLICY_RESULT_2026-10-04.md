# Scoped association authoring with saved task mode

Approved local selector and immutable task policy; branch
feature/quote-scoped-associations fromf0f0dae. No deployment/paidcalls.
Server BOM_SOURCE_ASSOCIATIONS_QUOTE_UUIDS defaults empty, strictUUID list/max100,
must fit budget scope unless globalbudgettrue. Configuration rejected at startup
before clients/server; global legacy flag remains supported, not enabled here.

Trusted budget membership supplies quoteUUID. Request-local dispatcher copies
prevent shared state mutation. Before any enrolled author dispatch, resolve saves
author-policy/v1/requests/<request>.json binding exact membershiphash/group/version
and BOTH legacy/v6 choices. First verified choice wins immutable conflicts;
saved mode explicitly overrides later serverglobal/allowlist changes on every
retry. Invalid/inaccessible records stop before reservation/model setup.
Existing budgethistory without policy holds; no guessed retrofit or A021 enrollment.
Unenrolled legacy behavior unchanged. Removing scope is not cancellation.

Tests54focused passed; final738fullpassed1SDKskip56.97sec; independentGOlocalonly.
Initial old exact-byte fixture3vs4 failuredidnotrecur; causeunconfirmed. Source
diffchecks pass. Additional racing publication/history-check test useful: loser
may failclosed and retry readlater, not dispatch a secondpolicy. Reviewer assessed
statically; no final liveconcurrency proof. No APIshape/client changes/UItests.

Rollback: revert focusedsourcecommit, retain unusedimmutablepolicyrecords; never
overwrite/delete them to reset mode/budget. Existing deploy81efb5e/selectorsfalse
unchanged. Combinedselectors now local only; pinnedimages/disabledrollout/UIcodes/
authenticatedcanary and paidbudget approvals still separate. Semantic field-role
acceptance gap remains; this does not approve output/import/CQ or generalcapacity.
Unrelated packageevidence dirtydocs preserved outside commit.