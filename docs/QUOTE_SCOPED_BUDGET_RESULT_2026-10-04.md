# Quote-scoped budget enrollment, local only

Approved local selector/tests, branchfeature/quote-scoped-budget/base81efb5e.
Server config BOM_AUTHOR_GROUP_BUDGET_QUOTE_UUIDS: comma-separated valid UUIDs,
max100 entries, normalizedcase/whitespace, emptyselectsnone, no wildcards/blank
entries/malformed values. main validates both globalflag and scope before setup.
No new API request field or user-supplied group selector.

Globalfalse with nonemptyallowlist enrolls freshmatchingquotes through trusted
parentplan. Perrequest predicate uses immutable set, not sharedbool mutation.
Unlistedfreshquotes keep legacybehavior; existingoptoutrequests not backfilled.
Globaltrue retains previous broad enrollment behavior; not enabled by this unit.
Removing/clearingallowlist does not bypass persistedmembership/requiredintent
accounting; automation discovers thosefacts independently of enrollmentflag.
Storageerrors propagate failclosed. This is enrollment scope, not a cancellation
or dispatchkillswitch for already-enrolledrequests.

Verification24focused/729fullpassed1SDKskip60.04sec; diffclean. Independentfollowup
GOlocalonly after correcting last-entryremoval enforcement and startupvalidation.
Initialfull test bytealias fixture4vs3 mismatch didnotrecur, causeunconfirmed.
No client/runtimeAPIshape changes so UI suite not rerun. Defaultempty docsadded.
No deployment/images/push/modelcalls/grants/import/CQ/hold/preference changes.

Associationauthorselector remains service-wide and separate; budgetscope alone
does not select v6 interpretation for justonequote. Combinedlivecanary needs
separate association scope design/approval, UIholdcode deployment/authprobe and
paidinput/budget plan. Semanticrole gap remains. Sequential selected/unlisted
isolation tested; true concurrent processing additionalcoverage useful.
Rollback: revert focusedcommit; configured empty/listfalse before deployment;
retain persistedbudgetevidence, never remove enrollmentrecords to reset budget.