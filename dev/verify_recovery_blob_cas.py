# Purpose: source-bound approval packet and synthetic native Blob CAS verification.
# Inputs: --execute and --approved-plan-sha256; existing Azure CLI login only.
# Outputs: JSON packet/results with safe codes; execution retains exactly two blobs.
# Run: python dev/verify_recovery_blob_cas.py (local dry-run, no authentication).
from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

ROOT = Path(__file__).resolve().parents[1]
PREFIX = "test-only/recovery-coordination/20261004/"
PATHS = (PREFIX + "create-race.json", PREFIX + "etag-race.json")
ACCOUNT_URL = "https://etbomstoragegov.blob.core.usgovcloudapi.net"
SUBSCRIPTION = "37d47e73-56ae-433b-a340-86eac088c78e"
TENANT = "a6664873-a1e6-4a7e-9d4b-de20bfa7dbd6"
SCOPE = "https://storage.azure.com/.default"
MAX_BYTES = 4096
TIMEOUT = 30
SDK_LIMITS = {"writes": 6, "properties": 9, "downloads": 5, "lists": 1}


class SafeFailure(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class AcknowledgementLost(Exception):
    pass


def canonical_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def fixed_plan() -> dict:
    return {
        "version": 1,
        "cloud": "AzureUSGovernment",
        "subscription": SUBSCRIPTION,
        "tenant": TENANT,
        "account_url": ACCOUNT_URL,
        "container": "parsers",
        "prefix": PREFIX,
        "paths": list(PATHS),
        "max_record_bytes": MAX_BYTES,
        "credential": "Azure CLI access token; memory-only; existing identity; no grants",
        "token_scope": SCOPE,
        "approval": "sha256 of canonical JSON containing fixed plan and exact source hashes",
        "operations": [
            "one bounded prefix-absence list, first page only, at most two entries",
            "two distinct create-only contenders, two clients, thread barrier",
            "fresh read confirms sole create winner",
            "one seed create on second path; fresh read captures ETag",
            "two distinct updates with same ETag, two clients, thread barrier",
            "fresh read confirms sole update winner",
            "one CAS to held owner; inject local acknowledgement loss after success",
            "fresh read confirms held owner; no retry or reset",
        ],
        "expected": {"write_attempts": 6, "successful_writes": 4, "conflicts": 2,
                     "explicit_reads": 4, "prefix_lists": 1, "retained_blobs": 2},
        "sdk_request_bounds": dict(SDK_LIMITS),
        "sdk_total_requests_max": sum(SDK_LIMITS.values()),
        "retry_total": 0,
        "request_timeout_seconds": TIMEOUT,
        "cli_process_timeout_seconds": TIMEOUT,
        "barrier_timeout_seconds": TIMEOUT,
        "prohibited": ["other paths", "deletes", "cleanup", "sleep", "poll", "reset",
                       "automatic retries", "new grants", "persisted tokens"],
    }


def approval_packet(runner_source: bytes | None = None, adapter_source: bytes | None = None) -> dict:
    sources = {
        "dev/verify_recovery_blob_cas.py": hashlib.sha256(
            Path(__file__).read_bytes() if runner_source is None else runner_source).hexdigest(),
        "bom_processor/core.py": hashlib.sha256(
            (ROOT / "bom_processor/core.py").read_bytes() if adapter_source is None else adapter_source).hexdigest(),
    }
    binding = {"plan": fixed_plan(), "source_sha256": sources}
    return {**binding, "approved_plan_sha256": hashlib.sha256(canonical_bytes(binding)).hexdigest()}


class Budget:
    def __init__(self):
        self.counts = dict.fromkeys(SDK_LIMITS, 0)
        self.lock = threading.Lock()

    def take(self, operation: str):
        with self.lock:
            if self.counts[operation] >= SDK_LIMITS[operation]:
                raise SafeFailure("REQUEST_BOUND_EXCEEDED")
            self.counts[operation] += 1


def guard_path(path: str):
    if path not in PATHS:
        raise SafeFailure("PATH_FORBIDDEN")


class GuardedBlob:
    def __init__(self, blob, budget: Budget):
        self.blob = blob
        self.budget = budget

    def get_blob_properties(self):
        self.budget.take("properties")
        return self.blob.get_blob_properties(timeout=TIMEOUT)

    def download_blob(self, **kwargs):
        if (kwargs.get("offset") != 0 or not 0 < kwargs.get("length", 0) <= MAX_BYTES + 1
                or kwargs.get("max_concurrency") != 1):
            raise SafeFailure("READ_BOUND_FORBIDDEN")
        self.budget.take("downloads")
        return self.blob.download_blob(**kwargs, timeout=TIMEOUT)

    def upload_blob(self, data: bytes, **kwargs):
        if not isinstance(data, bytes) or not 0 < len(data) <= MAX_BYTES:
            raise SafeFailure("RECORD_BOUND_FORBIDDEN")
        if kwargs.get("overwrite") is not False and not (
                kwargs.get("overwrite") is True and kwargs.get("etag")
                and kwargs.get("match_condition") is not None):
            raise SafeFailure("WRITE_CONDITION_FORBIDDEN")
        self.budget.take("writes")
        return self.blob.upload_blob(data, **kwargs, timeout=TIMEOUT)


class GuardedContainer:
    def __init__(self, container, budget: Budget):
        self.container = container
        self.budget = budget

    def get_blob_client(self, path: str):
        guard_path(path)
        return GuardedBlob(self.container.get_blob_client(path), self.budget)

    def list_blobs(self, *, name_starts_with: str):
        if name_starts_with != PREFIX:
            raise SafeFailure("PREFIX_FORBIDDEN")
        self.budget.take("lists")
        pages = self.container.list_blobs(name_starts_with=PREFIX, results_per_page=2,
                                         timeout=TIMEOUT).by_page()
        first_page = next(pages, ())
        entries = tuple(itertools.islice(first_page, 2))
        if entries:
            raise SafeFailure("PREFIX_NOT_EMPTY")
        if getattr(pages, "continuation_token", None):
            raise SafeFailure("PREFIX_ABSENCE_UNCONFIRMED")
        return entries


class GuardedStorage:
    def __init__(self, adapter):
        self.adapter = adapter

    def list_registry_paths(self, prefix: str, max_results: int):
        if prefix != PREFIX or max_results != 1:
            raise SafeFailure("PREFIX_FORBIDDEN")
        try:
            return self.adapter.list_registry_paths(prefix, max_results)
        except Exception as failure:
            if isinstance(failure.__cause__, SafeFailure):
                raise failure.__cause__ from None
            raise

    def download_registry_with_etag(self, path: str, max_bytes: int):
        guard_path(path)
        if max_bytes != MAX_BYTES:
            raise SafeFailure("READ_BOUND_FORBIDDEN")
        return self.adapter.download_registry_with_etag(path, max_bytes)

    def upload_registry_if_match(self, path: str, data: bytes, content_type: str, etag: str | None):
        guard_path(path)
        if not isinstance(data, bytes) or not 0 < len(data) <= MAX_BYTES or content_type != "application/json":
            raise SafeFailure("RECORD_BOUND_FORBIDDEN")
        return self.adapter.upload_registry_if_match(path, data, content_type, etag)


def adapter_class():
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from bom_processor.core import AzureBlobStorage
    return AzureBlobStorage


def wrap_container(container, budget: Budget) -> GuardedStorage:
    adapter = adapter_class().__new__(adapter_class())
    adapter._parsers = GuardedContainer(container, budget)
    adapter._sources = None
    return GuardedStorage(adapter)


def load_live_clients():
    try:
        from azure.core.credentials import AccessToken
        from azure.storage.blob import BlobServiceClient
    except ImportError:
        raise SafeFailure("REQUIRED_PACKAGES_UNAVAILABLE") from None
    try:
        launcher = shutil.which("az")
        if not launcher:
            raise SafeFailure("CLI_CONTEXT_UNAVAILABLE")
        def cli(arguments):
            # Fixed literal CLI arguments only; .cmd launch on Windows needs shell.
            response = subprocess.run([launcher, *arguments], capture_output=True,
                                      timeout=TIMEOUT, check=True, shell=os.name == "nt")
            return json.loads(response.stdout)
        context = cli(["account", "show", "--output", "json"])
        cloud = cli(["cloud", "show", "--output", "json"])
    except Exception:
        raise SafeFailure("CLI_CONTEXT_UNAVAILABLE") from None
    if (context.get("id") != SUBSCRIPTION or context.get("tenantId") != TENANT
            or cloud.get("name") != "AzureUSGovernment"):
        raise SafeFailure("CLI_CONTEXT_MISMATCH")
    try:
        response = cli(["account", "get-access-token", "--resource", "https://storage.azure.com/", "--output", "json"])
        token = AccessToken(response["accessToken"], int(response["expires_on"]))
    except Exception:
        raise SafeFailure("CLI_TOKEN_UNAVAILABLE") from None

    class MemoryCredential:
        def get_token(self, *scopes, **kwargs):
            if scopes != (SCOPE,) or token.expires_on <= time.time() + TIMEOUT:
                raise SafeFailure("TOKEN_SCOPE_OR_EXPIRY")
            return token

    services = []
    budget = Budget()
    try:
        for unused in range(2):
            service = BlobServiceClient(account_url=ACCOUNT_URL, credential=MemoryCredential(),
                                        retry_total=0, retry_connect=0, retry_read=0, retry_status=0,
                                        connection_timeout=TIMEOUT, read_timeout=TIMEOUT,
                                        logging_enable=False)
            services.append(service)
        clients = [wrap_container(service.get_container_client("parsers"), budget) for service in services]
    except Exception:
        for service in services:
            service.close()
        raise SafeFailure("CLIENT_CONFIGURATION_FAILED") from None
    return clients, budget, services


def record(owner: str, state: str) -> bytes:
    return canonical_bytes({"synthetic": True, "owner": owner, "state": state, "version": 1})


def verify(clients: list, budget: Budget) -> dict:
    from bom_processor.core import BlobConflictError
    counts = {"write_attempts": 0, "successful_writes": 0, "conflicts": 0,
              "explicit_reads": 0, "prefix_lists": 0, "retained_blobs": 0}
    lock = threading.Lock()

    def write(client, path, data, etag):
        with lock:
            if counts["write_attempts"] >= 6:
                raise SafeFailure("WRITE_BOUND_EXCEEDED")
            counts["write_attempts"] += 1
        result = client.upload_registry_if_match(path, data, "application/json", etag)
        with lock:
            counts["successful_writes"] += 1
        return result

    def read(path):
        if counts["explicit_reads"] >= 4:
            raise SafeFailure("READ_BOUND_EXCEEDED")
        counts["explicit_reads"] += 1
        return clients[0].download_registry_with_etag(path, MAX_BYTES)

    def race(path, etag, state):
        barrier = threading.Barrier(2, timeout=TIMEOUT)

        def contender(index):
            data = record("owner-" + str(index), state)
            barrier.wait()
            try:
                write(clients[index], path, data, etag)
                return "success", data
            except BlobConflictError:
                with lock:
                    counts["conflicts"] += 1
                return "conflict", None

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(contender, index) for index in range(2)]
            outcomes = [future.result() for future in futures]
        if sorted(outcome[0] for outcome in outcomes) != ["conflict", "success"]:
            raise SafeFailure("RACE_WINNER_MISMATCH")
        return next(data for status, data in outcomes if status == "success")

    counts["prefix_lists"] += 1
    if clients[0].list_registry_paths(PREFIX, 1):
        raise SafeFailure("PREFIX_NOT_EMPTY")
    winner = race(PATHS[0], None, "created")
    if read(PATHS[0])[0] != winner:
        raise SafeFailure("CREATE_WINNER_MISMATCH")
    seed = record("seed", "ready")
    write(clients[0], PATHS[1], seed, None)
    seeded, etag = read(PATHS[1])
    if seeded != seed:
        raise SafeFailure("SEED_MISMATCH")
    winner = race(PATHS[1], etag, "updated")
    updated, etag = read(PATHS[1])
    if updated != winner:
        raise SafeFailure("CAS_WINNER_MISMATCH")
    held = record("held-owner", "held")
    try:
        write(clients[0], PATHS[1], held, etag)
        raise AcknowledgementLost()
    except AcknowledgementLost:
        if read(PATHS[1])[0] != held:
            raise SafeFailure("ACK_LOSS_OWNER_MISMATCH")
    counts["retained_blobs"] = 2
    if counts != fixed_plan()["expected"] or budget.counts != SDK_LIMITS:
        raise SafeFailure("OPERATION_COUNTS_MISMATCH")
    return {"code": "VERIFIED_SYNTHETIC_CAS", "counts": counts,
            "sdk_requests": dict(budget.counts), "sdk_total_requests": sum(budget.counts.values()),
            "acknowledgement_loss": "injected locally; fresh read confirmed held owner"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Source-bound synthetic Blob CAS approval packet")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--approved-plan-sha256")
    args = parser.parse_args(argv)
    services = []
    try:
        packet = approval_packet()
        if not args.execute:
            print(json.dumps({"code": "DRY_RUN_ONLY", **packet}, sort_keys=True, indent=2))
            return 0
        if args.approved_plan_sha256 != packet["approved_plan_sha256"]:
            raise SafeFailure("EXACT_SOURCE_APPROVAL_REQUIRED")
        clients, budget, services = load_live_clients()
        result = verify(clients, budget)
        print(json.dumps({**result, "approved_plan_sha256": packet["approved_plan_sha256"]}, sort_keys=True))
        return 0
    except SafeFailure as failure:
        print(json.dumps({"code": failure.code}, sort_keys=True))
        return 2
    except Exception:
        print(json.dumps({"code": "VERIFICATION_STOPPED"}, sort_keys=True))
        return 2
    finally:
        for service in services:
            try:
                service.close()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())