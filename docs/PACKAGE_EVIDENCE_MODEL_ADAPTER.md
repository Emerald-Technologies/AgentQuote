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

Policy `package-evidence-foundry.v6` (plan 8.8 step 5, Nick 2026-10-09;
`bom_processor/package_evidence_compact.py`): each BOM line is sent once with short ids
(A1, L1, F1); Gerber and drill files show name, folder, type, size and header lines only;
other text is whole up to 50 lines, otherwise the first 40 and last 10. Every quote is
checked against the full file. Each producer or reviewer call is at most 256 KB including
the instructions, with a 16,384-token reply and a 300-second deadline. A package that does
not fit is split per assembly (files placed by part numbers in their path; the rest go to
every call), an oversized assembly into blocks of lines. Replies are grouped answers that
code expands to one record per line; each call is checked and retried once. A call that
fails twice leaves its lines and own files open (`EVIDENCE_CALL_FAILED`) while the rest is
kept. Checked answers are stored under `package-evidence-calls/` by a hash of the call
input, so a rerun repeats only failed or changed calls. Stored artifacts keep the v5 shapes.
The v5 prompt text is kept as `V5_INSTRUCTION` (its hash is the one on existing results).

Packet and response accepted size: two million UTF-8 bytes each (v6 calls are also held to
the 256 KB input ceiling). Request deadline defaults to 300 seconds (v6).
Provider-side SDK retries/cost settings are not verified by offline tests. Unsupported
response forms fail closed. Graph review/approval is still a separate human boundary.

Verification: full offline suite 444 passed, including 29 adapter tests; factory SDK
and credentials are doubles. Independent security review GO for local commit only.

Before a live run: confirm cloud/tenant/resource/model deployment, package data scope,
token/output/cost limits, SDK behavior, and specific invocation approval. No customer
sample has been sent to a model. PDF/binary workbook readers, persistent producer
artifacts, HTTP/job wiring, graph review and processing remain later checkpoints.