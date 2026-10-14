"""Operator CLI: submit a hand-written parser as the candidate for an existing live candidate request.

Purpose: when the model has not produced a usable parser, a person supplies one. The parser goes
through the normal authoring boundary (same code policy, same immutable parse.py + manifest +
generated event) and one immutable operator selection record makes the status projection choose
it. Validation then runs as for any candidate (QuickQuote's next status poll starts the normal
validator job) and a person still approves the BOM in QuickQuote. No QuickQuote UI or API change;
nothing is approved, promoted or skipped here.

Commands (all read-only unless --execute is given to submit):
  lookup  --rfq UUID [--since-hours 72]       list candidate requests whose sources belong to the quote
  status  --request-id ID                     the request's current status projection (no automation)
  check   --request-id ID (--parser P | --config C)
                                              download the request's sources (hash-verified) and run the
                                              parser through the real offline validator on each one
  submit  --request-id ID (--parser P | --config C) --actor ID --reason TEXT [--execute]
                                              check first (refuses unless every source passes), then
                                              print (dry run) or write the candidate and selection record

Inputs: BOM_STORAGE_ACCOUNT_URL (or --account-url), the operator's `az login` (AzureCliCredential,
Azure Government cloud set in the CLI). Outputs: JSON on stdout; customer rows are never printed.
Writes (submit --execute only), all write-once in the parsers container:
  candidates/<request>/<candidate>/parse.py, candidate-manifest.json,
  candidates/events/<request>/<candidate>.json, candidates/operator-selections/<request>/<candidate>.json
Rollback: delete the selection record (status returns to the budget/rank choice); for a request
without an author budget also delete the three candidate blobs, or status sees two candidates.
Run: python dev/operator_parser_candidate.py lookup --rfq <uuid>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bom_processor.core import BlobConflictError, BlobNotFoundError  # noqa: E402
from bom_processor.fixed_layout_parser import build_parser  # noqa: E402
from bom_processor.operator_candidate import apply, offline_validate  # noqa: E402

MAX_SOURCE_BYTES = 50_000_000


class RecordingStorage:
    """Live reads, captured writes: a dry run sees exactly what --execute would write."""
    def __init__(self, live: Any):
        self.live, self.writes = live, {}

    def _read(self, path: str, reader: Any, max_bytes: int) -> bytes:
        if path in self.writes:
            return self.writes[path][0]
        return reader(path, max_bytes)

    def download_registry(self, path: str, max_bytes: int) -> bytes:
        return self._read(path, self.live.download_registry, max_bytes)

    def download_candidate(self, path: str, max_bytes: int) -> bytes:
        return self._read(path, self.live.download_candidate, max_bytes)

    def download_source(self, path: str, max_bytes: int) -> bytes:
        return self.live.download_source(path, max_bytes)

    def _write(self, path: str, data: bytes, content_type: str, reader: Any) -> None:
        try:
            existing = reader(path, len(data) + 1)
        except (BlobNotFoundError, FileNotFoundError):
            existing = None
        if existing is not None and existing != data:
            raise BlobConflictError(f"would conflict with existing blob {path}")
        self.writes[path] = (data, content_type, "exists-identical" if existing is not None else "new")

    def upload_candidate_immutable(self, path: str, data: bytes, content_type: str) -> None:
        self._write(path, data, content_type, self.live.download_candidate)

    def upload_registry_immutable(self, path: str, data: bytes, content_type: str) -> None:
        self._write(path, data, content_type, self.live.download_registry)

    def list_registry_paths(self, prefix: str, max_results: int) -> list[str]:
        return sorted(set(self.live.list_registry_paths(prefix, max_results))
                      | {path for path in self.writes if path.startswith(prefix)})

    def list_candidate_paths(self, prefix: str, max_results: int) -> list[str]:
        return sorted(set(self.live.list_candidate_paths(prefix, max_results))
                      | {path for path in self.writes if path.startswith(prefix)})


def live_storage(account_url: str):
    from azure.identity import AzureCliCredential
    from bom_processor.core import AzureBlobStorage
    if not account_url:
        raise SystemExit("set BOM_STORAGE_ACCOUNT_URL or pass --account-url")
    return AzureBlobStorage.from_account_url(account_url, AzureCliCredential())


def parser_code(args: argparse.Namespace) -> str:
    if args.config:
        return build_parser(json.loads(Path(args.config).read_text(encoding="utf-8")))
    return Path(args.parser).read_text(encoding="utf-8")


def request_record(storage: Any, request_id: str) -> dict[str, Any]:
    return json.loads(storage.download_registry(f"candidates/requests/{request_id}.json", 1_000_000))


def sources(storage: Any, request: dict[str, Any]) -> list[tuple[dict[str, Any], bytes]]:
    from bom_processor.candidates import unique_fixtures
    result = []
    for fixture in unique_fixtures(request["source_evidence"]):
        data = storage.download_source(fixture["blob_path"], MAX_SOURCE_BYTES)
        if len(data) != fixture["size_bytes"] or hashlib.sha256(data).hexdigest() != fixture["sha256"].lower():
            raise SystemExit(f"source integrity check failed for {fixture['blob_path']}")
        result.append((fixture, data))
    return result


def check(storage: Any, request_id: str, code: str) -> list[dict[str, Any]]:
    request = request_record(storage, request_id)
    if request.get("status") != "requested":
        raise SystemExit("candidate request is not in status 'requested'")
    results = []
    for fixture, data in sources(storage, request):
        summary = offline_validate(data, PurePosixPath(fixture["blob_path"]).suffix.lower(), code)
        summary.pop("rows", None)
        summary["source"] = PurePosixPath(fixture["blob_path"]).name
        summary["source_sha256"] = fixture["sha256"]
        results.append(summary)
    return results


def status(storage: Any, request_id: str) -> dict[str, Any]:
    from bom_processor.candidate_status import CandidateStatusService
    value = CandidateStatusService(storage).get(request_id)
    value.pop("review", None)
    return value


def lookup(account_url: str, rfq: str, since_hours: int) -> list[dict[str, Any]]:
    """Requests created in the window whose source paths belong to this quote (read-only scan)."""
    from azure.identity import AzureCliCredential
    from azure.storage.blob import BlobServiceClient
    container = BlobServiceClient(account_url, credential=AzureCliCredential()).get_container_client("parsers")
    since = datetime.now(timezone.utc) - timedelta(hours=since_hours)
    marker = f"/rfq-{rfq.lower()}/"
    found = []
    for blob in container.list_blobs(name_starts_with="candidates/requests/"):
        if blob.last_modified < since:
            continue
        value = json.loads(container.get_blob_client(blob.name).download_blob(max_concurrency=1).readall())
        paths = [item.get("blob_path", "") for item in value.get("source_evidence", [])]
        if any(marker in path for path in paths):
            found.append({"request_id": value["request_id"], "created_at": value.get("created_at"),
                          "sheets": value.get("profile", {}).get("sheet_names"),
                          "sources": [PurePosixPath(path).name for path in paths]})
    return found


def main(argv: list[str] | None = None) -> int:
    import os
    parser = argparse.ArgumentParser(description="Operator parser candidates (see module docstring).")
    parser.add_argument("--account-url", default=os.environ.get("BOM_STORAGE_ACCOUNT_URL", ""))
    commands = parser.add_subparsers(dest="command", required=True)
    find = commands.add_parser("lookup")
    find.add_argument("--rfq", required=True)
    find.add_argument("--since-hours", type=int, default=72)
    show = commands.add_parser("status")
    show.add_argument("--request-id", required=True)
    for name in ("check", "submit"):
        command = commands.add_parser(name)
        command.add_argument("--request-id", required=True)
        source = command.add_mutually_exclusive_group(required=True)
        source.add_argument("--parser")
        source.add_argument("--config")
        if name == "submit":
            command.add_argument("--actor", required=True)
            command.add_argument("--reason", required=True)
            command.add_argument("--execute", action="store_true",
                                 help="write to live storage; without it nothing is written")
    args = parser.parse_args(argv)
    if args.command == "lookup":
        print(json.dumps(lookup(args.account_url, args.rfq, args.since_hours), indent=1))
        return 0
    storage = live_storage(args.account_url)
    if args.command == "status":
        print(json.dumps(status(storage, args.request_id), indent=1, default=str))
        return 0
    code = parser_code(args)
    results = check(storage, args.request_id, code)
    print(json.dumps({"offline_validation": results}, indent=1, default=str))
    if any(item.get("state") != "awaiting_approval" for item in results):
        print("refused: the parser does not pass offline validation on every source", file=sys.stderr)
        return 1
    if args.command == "check":
        return 0
    # Same steps on a recording view first: the dry run and the execute plan are identical.
    plan = RecordingStorage(storage)
    authored, record = apply(plan, args.request_id, code, actor=args.actor, reason=args.reason,
                             hold_writer=False)
    planned = [{"path": path, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                "content_type": content_type, "state": state}
               for path, (data, content_type, state) in sorted(plan.writes.items())]
    before = status(storage, args.request_id)
    print(json.dumps({"mode": "execute" if args.execute else "dry-run", "request_id": args.request_id,
                      "candidate_id": authored.candidate_id, "code_sha256": record["code_sha256"],
                      "status_before": {key: before.get(key) for key in ("status", "candidate_id", "validation_id",
                                                                           "automation_error")},
                      "writes": planned,
                      "selection_record": {key: value for key, value in record.items()
                                           if key not in {"selected_time", "selection_hash"}}}, indent=1))
    if not args.execute:
        return 0
    authored_live, _ = apply(storage, args.request_id, code, actor=args.actor, reason=args.reason)
    if authored_live.candidate_id != authored.candidate_id:
        raise SystemExit("candidate id changed between the dry run and the write")
    after = status(storage, args.request_id)
    print(json.dumps({"status_after": {key: after.get(key) for key in ("status", "candidate_id", "validation_id",
                                                                       "automation_error")}}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
