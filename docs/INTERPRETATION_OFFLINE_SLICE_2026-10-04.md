# Offline interpretation coverage slice

Approved first slice of QuickQuote coverage design; branch
feature/bom-interpretation-offline-verifier fromb12170d. No runtime imports/factory
wiring, models, jobs, storage, approval or deployment changes. Earlier quantity/
identity checkpoint remains independently nondeployable. Dirty existingdocs retained.

New interpretation_inventory reads bounded inert XLSX and HTML-disguisedXLS,
all supported sheets/populatedcells/mergedanchors; macros/externallinks/formulas/
errors rejected explicitly. ZIP expansion and pre-openpyxl rectangular traversal
budget100000 prevent sparse-sheet materialization exhaustion. BinaryXLS rejected
before allocation (cached-formula provenance unavailable); XLSM unsupported.
This is narrower than all ordinaryExcelformats, not sourceBOM rejection policy.

New interpretation_coverage strictJSON sidecar checks source/outputhashes/sizes,
sourcecells/direct outputvalue links, exact disposition inventory, outputrecords,
manufacturerpair completeness, quantity provenance and duplicates. Outputrelations
are agent proposals. Semantic acceptance alwaysnot_evaluated; authorizationfalse.
Unrepresented semanticcells become explicitblockinggaps, not clean preservation.
Structuraldispositions are untrusted claims: cannotprove grouping/category meaning.
Same-row manufacturerpairlinks and directcopyquantities only; unsupportedtransform/
splitpair evidence needs later findings/contracts, no automaticinterpretation.
No association/modelidentity fields implemented in this minimalstrictsubset.

Verification:22focusedtests/579fullproducerpassed. Syntheticverticalalternates:
Aqty1,Dqty5,twopairs each plusnonpricedDocument optionalquantity. Tamper/hash/
coverage/type/formula/sparse/merged/binaryunsupported cases covered. Independent
review initialNO-GO3findings corrected (preallocationbounds/reverseaccounting/types),
no independentpostcorrectionGO claimed. Security/coveragechecks not universalproof.

Limits:1MiBsidecar/32MiBsource/256ZIPentries/8MiBentry/32sheets/100000rows/
512columns/100000populatedcells AND rectangulartraversalbudget/1000findings.
Large/unsupportedsource returns explicitInventoryError, never partialsuccess.
No actualchunktools/worker-sidecartransport/Arenaheuristicreplacement implemented.
No releaseorautonomouspipelineGO; next slice requires separate approval.

Rollback revertfocusednewmodules/tests/docs; no schema/data change. Existing
modelUSD10allowance unchanged. Use syntheticfixtures only; tests are not liveanswers.