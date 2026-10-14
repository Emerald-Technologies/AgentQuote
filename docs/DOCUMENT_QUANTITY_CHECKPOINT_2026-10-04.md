# Document quantities and Arena references — incomplete source checkpoint

Nick approved document quantities as nonpriced evidence and Arena policy alignment.
Scope AgentQuote source only; no deployments/registry promotions/models/live jobs.
Branch fix/document-quantity-validation from55b5ea3; fetchedmainancestor confirmed.
Existing dirty PACKAGE_EVIDENCE_JOBS/MODEL_ADAPTER docs preserved unstaged.

## Changed

Workbook schema allows missing Document quantity, retains finite supplied values;
Part checks unchanged. Candidate quantity positivity applies to Part, not Document.
Semantic comparison ignores Document quantity while retaining identity/revision.
Arena Drawing/Procedure/Document/Specification references retained as Document,
instead of zero-quantity omission; original narrow empty-parent rule unchanged.
Source absent/numeric document quantity preservation enforced independently of
material comparison. Policies arena-html-policy.v2/candidate-authoring.v5;
historical immutable artifacts untouched. No pricing/database/security change.

## Verified and remaining blocker

Offline initial reproductions: blankDocument workbook quantityinvalid; candidate
quantityfailure; Document quantity-only semanticconflict. Focused87passed;
full final557passed; downstreamQuickQuoteFunctions102passed/2fixturewarnings.
Full first run had2old-authoring-version path expectations, corrected forv5.

Independent review NO-GO identified quantity-preservation gap (corrected/tested)
and multiple-document assembly inference failure (not corrected). Actual integration
withDRAW-1/PROC-1/Part returnedsemantic_output_invalid/assembly_identity_conflict.
semantic_facts.document_assembly treats every Document item_number/revision as
assembly declaration, so retained distinct references conflict. Current tests pass
but this reproduced integration blocker means checkpoint is NOT ready to deploy.

Requires approved assembly-declaration contract: authoritative package/assembly
binding versus explicit source declaration evidence; no first-row/name heuristic,
dropping documents or fabricated identity. Preserve failclosed conflict checks for
actual competing assembly declarations. Existing demo Document rows contain assembly
identity; ordinary drawings/procedures can carry their own identifiers. Do not
decide their meaning solely from categoryDocument.

Rollback: revert focused source commit; no data/schema rollback. New policy versions
change candidate identity/attempt namespaces; old approvals remain historical,
not silently revalidated. Semantic fingerprints for Document quantities change;
fresh checkpoint validation required before downstream reuse. No runtime GO.