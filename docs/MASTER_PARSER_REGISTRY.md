# Master parser registry

Quick Quote supplies one customer `parser_key`. AgentQuote profiles bounded BOM
structure and evaluates only approved variants belonging to that key. Exactly
one signature must match. Zero matches return `PARSER_FORMAT_NOT_RECOGNIZED`;
multiple matches return `PARSER_FORMAT_AMBIGUOUS`. Priority never hides overlap.

Candidate Python is immutable evidence under `candidates/` and is never imported,
compiled, or executed by the registry. First-time approval binds customer,
parser key, candidate and code hashes, signature/rule hashes, fixture input and
result hashes, variant version, approver, and timestamp. Promotion copies the
exact bytes to `approved/<parser>/<variant>/<version>/parse.py`. Any changed
binding requires a new approval.

The local store reloads and verifies its registry, signatures, and approved code
after restart. It is for single-process development only. Production persistence
must use Blob conditional writes, ETag comparison, durable locking, and a
separately reviewed promotion boundary.

Profiling bounds source size, ZIP entries, decompressed XML parts, total expanded
size, compression ratios, sheet/header counts, and normalized header length.
Public errors expose stable codes and bounded identifiers/hashes, not local paths
or full profile contents.

## Candidate authoring boundary

Authoring reads one exact requested record and exact source evidence, supplies
only bounded profile metadata to an injected model, and statically validates
generated UTF-8 Python. It writes immutable candidate evidence and a generated
event only. Candidates are never imported, executed, activated, approved, or
added to the master registry; promotion remains a separate review boundary.

## Candidate requests

When an existing registry has no matching approved variant, processing creates a
versioned immutable record at `candidates/requests/<request_id>.json`. The ID is
deterministic over `parser_key`, `customer_key`, and the bounded structural
profile hash, so equivalent retries deduplicate. The record contains bounded
structural evidence and the first source document's integrity metadata; source
bytes and candidate code are never executed. Multi-document requests require
the same structural profile and reject mismatched variants. The API returns only
the request ID, parser key, and profile hash with HTTP 202.