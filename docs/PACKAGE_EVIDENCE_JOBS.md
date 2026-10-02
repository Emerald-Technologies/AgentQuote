# Evidence request/artifact job seam

Status: isolated local implementation; no HTTP route, Azure factory or live job.
User approved scoped AgentQuote edits on 2026-10-01. Retains existing BOM behavior.

`bom_processor/package_evidence_jobs.py` provides:

- `evidence_request`: strict initial checkpoint/assemblies, pinned root/workbook
  references, explicit producer/model/policy/prompt metadata, deterministic request ID.
- `EvidenceJobService.submit`: immutable request persistence only.
- `start`: injected starter receives only REQUEST_ID; unique immutable claim ensures
  one concurrent owner with atomic storage. Unknown start is never automatically retried.
- `status`: side-effect-free artifact reads; validates strict lifecycle markers,
  request hash, completed artifact/proposal hashes and checkpoint/provenance binding.
- `run`: unique worker claim, independently verified staged bytes, existing offline
  producer with injected model, immutable proposal/completion publication. No approval.
- `reconcile_publication`: explicit recovery of an already-published proposal only,
  no model invocation, no claim stealing/deletion. Publication uncertainty does not
  become terminal execution failure. Missing proposal requires operator reconciliation.

Paths use exact `evidence-inputs/<quote>/package-<version>/<document>/<sha256>`
references, not arbitrary URLs/SAS. Registry artifacts live under
`package-evidence/<requestId>/`. These namespace choices are local contracts; staging
and HTTP consumers are not wired. Production integration must authenticate callers,
validate checkpoint ownership, constrain model/policy to actual server configuration
and verify source/inventory completeness independently. Request assertions alone are
not authority or evidence of actual model settings. QuickQuote re-verifies import.

Storage must guarantee atomic immutable create-or-verify, typed not-found and bounded
reads. Nonces distinguish claim owners under create-or-verify. Interruptions retaining
claims fail closed; no automatic recovery/retries or billed reexecution. No status poll
starts jobs. Partial/uncertain writes need explicit inspection/reconciliation.

Exception-only review target is retained. This unit creates proposals, not acceptance.
Qualifying packages eventually continue without human intervention using positive
policy check evidence and distinct system authority; unknowns/unproven exclusions/
ambiguous sharing remain review cases. No fabricated human approval attribution.

Verification on Python 3.10.6: 26 focused job tests, full offline suite470 passed.
Independent review initially NO-GO for marker corruption/publication stranding;
corrections and before/after-write failure regressions pass. No second independent
review claimed. No native Azure/job/storage/model/API/client/UI canary executed.
No dependencies, existing runtime changes, live data, permissions or deployment.

Next: approved authenticated HTTP/job entrypoint + QuickQuote staging/retrieval;
server-owned model/policy binding; native adapter/SDK budget checks; actual producer
through QuickQuote byte verifier; explicit live migration/model/pilot action gates.