# Package evidence model adapter

Status: isolated adapter, disabled by default. No runtime route/job wiring.

`FoundryEvidenceProposalModel` consumes the verified bounded packet and returns
strict JSON hints; the producer validates IDs, coverage, graph identity and approval
separation. `produce()` binds the actual system-instruction hash and model/policy
metadata. No tools, code execution, raw provider logging, or adapter retries.

Factory prerequisites (setting values never recorded here):

- `PACKAGE_EVIDENCE_MODEL_ENABLED=true` (explicit opt-in).
- `FOUNDRY_PROJECT_ENDPOINT`: HTTPS Government `.azure.us` endpoint.
- `PACKAGE_EVIDENCE_MODEL_DEPLOYMENT`: explicit deployment, no default.
- Optional `AZURE_CLIENT_ID` for existing managed identity.

Packet and response accepted size: two million UTF-8 bytes each. Request deadline
defaults to 120 seconds, configurable in the adapter constructor up to 300 seconds.
Provider-side SDK retries/cost settings are not verified by offline tests. Unsupported
response forms fail closed. Graph review/approval is still a separate human boundary.

Verification: full offline suite 444 passed, including 29 adapter tests; factory SDK
and credentials are doubles. Independent security review GO for local commit only.

Before a live run: confirm cloud/tenant/resource/model deployment, package data scope,
token/output/cost limits, SDK behavior, and specific invocation approval. No customer
sample has been sent to a model. PDF/binary workbook readers, persistent producer
artifacts, HTTP/job wiring, graph review and processing remain later checkpoints.