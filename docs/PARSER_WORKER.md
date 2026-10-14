# Parser worker

`Dockerfile.worker` builds an image containing `worker/` and the approved spreadsheet parser dependencies (`pandas`, `openpyxl`, and `xlrd`). The entry point runs the baked synthetic CSV fixture through the approved synthetic parser and writes one compact JSON result to stdout. The worker has stable result codes: `0` success, `2` fixture/size policy failure, `3` timeout or byte limit, `4` parser nonzero exit, and `5` invalid output.

The callable `worker.runtime.run_job` accepts synthetic parser and input bytes for tests; the CLI does not accept arbitrary paths. It copies bytes into a private temporary directory, rejects symlinks and paths outside that directory, uses an allowlisted environment, no shell, private cwd, `stdin=DEVNULL`, and a new Unix process session. It monitors runtime, output, and logs, and terminates the process group. Unix resource limits are best-effort.

Output must be deterministic compact JSON with schema `agentquote.synthetic.v1` and a `rows` list. Results include hashes/sizes for parser, input, output, and logs, plus configured limits. Logs are never included verbatim.

Synthetic mode remains the default. Remote mode uses the `opaque` output policy:
parser output is not schema-parsed or rewritten, but must be a regular, non-empty
file within the configured output limit. The initial policy permits only
`WORKER_OUTPUT_NAME=output.xlsx` and the exact XLSX MIME type
`application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`. The manifest
records that content type and the output upload uses it.

## Limits and limitations

There is no identity, credential, secret, Azure SDK, or network integration. The environment excludes inherited Azure, proxy, and secret variables. This is **not** a network sandbox: a parser can still use whatever network access the container provides. Filesystem and kernel isolation are incomplete; temporary-directory containment, process cleanup, and Unix rlimits reduce risk but do not replace container policy, seccomp, read-only mounts, or a dedicated sandbox. Windows does not provide the Unix process-group and rlimit guarantees.

## Local SAS transfer mode

The service-side `bom_processor.job_orchestrator` validates source and parser
bytes before issuing SAS tokens. It uses one deterministic
`processed/<job-id>/` prefix, read-only SAS for input/parser, and create/write
SAS for output/result. Tokens start one minute before issuance and expire after
15 minutes; HTTPS is required. The returned orchestration value contains only
the job ID, execution name, and expiry (never URLs or tokens).

The service identity needs a custom role assignment granting
`Microsoft.Storage/storageAccounts/blobServices/generateUserDelegationKey/action`
at the storage-account scope. It also needs **Container Apps Jobs Operator** at
the specific Container Apps Job resource scope (not subscription-wide). The
Gov ARM audience is `https://management.usgovcloudapi.net/.default`, and the
start endpoint is the Gov `Microsoft.App/jobs/<job>/start` endpoint. The worker
environment names are fixed: `WORKER_JOB_ID`, `INPUT_URL`, `PARSER_URL`,
`OUTPUT_URL`, `RESULT_URL`, `INPUT_SHA256`, `INPUT_SIZE`, `PARSER_SHA256`, and
`PARSER_SIZE`, `WORKER_OUTPUT_NAME`, and `WORKER_OUTPUT_CONTENT_TYPE`.

When all transfer variables (`WORKER_JOB_ID`, `INPUT_URL`, `PARSER_URL`, `OUTPUT_URL`,
`RESULT_URL`, and the input/parser SHA-256 and size values) are present, the entry point
uses HTTPS SAS URLs to download the parser and input, verifies exact sizes and hashes,
runs the same `run_job` callable in opaque mode, and writes output and a result manifest with
`If-None-Match: *`. A 409/412 is treated as a conflict and never overwrites an object.
URLs and SAS queries are not included in manifests, logs, or exception messages.
Unknown or partial `WORKER_` configuration is rejected before transfers. The SAS
environment is visible to the worker/control plane, but is intentionally not passed
to the child parser and is not logged.

Tests use injected bytes only through the callable API and cover successful execution, environment filtering, timeout/process-group cleanup, nonzero exit, output/log limits, and malformed output. They do not claim that arbitrary filesystem access or networking is blocked; those controls require a stronger platform sandbox and network policy.
