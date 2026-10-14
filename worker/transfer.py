"""SAS URL transfer protocol for the isolated worker (stdlib only)."""
from __future__ import annotations

import hashlib
import json
import os
import re
import socket
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

REQUIRED = ("WORKER_JOB_ID", "INPUT_URL", "PARSER_URL", "OUTPUT_URL", "RESULT_URL",
            "INPUT_SHA256", "INPUT_SIZE", "PARSER_SHA256", "PARSER_SIZE", "WORKER_INPUT_NAME",
            "WORKER_OUTPUT_NAME", "WORKER_OUTPUT_CONTENT_TYPE")
ALLOWED_WORKER = {"WORKER_JOB_ID", "WORKER_INPUT_NAME", "WORKER_OUTPUT_NAME", "WORKER_OUTPUT_CONTENT_TYPE",
                  "WORKER_INTERPRETATION_NAME", "WORKER_ROW_MAP_NAME"}
OUTPUT_CONTENT_TYPES = {
    "output.xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


def _url(value: str) -> str:
    try:
        p = urlsplit(value)
        if p.scheme != "https" or not p.hostname or not p.path or not p.query or p.fragment or p.username or p.password:
            raise ValueError
        if any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError
        return value
    except Exception as exc:
        raise ValueError("invalid transfer URL") from None


def redact(value: object) -> str:
    """Safe diagnostic representation; never returns a query or URL."""
    if isinstance(value, str) and "://" in value:
        try:
            p = urlsplit(value)
            return f"{p.scheme}://{p.hostname or ''}{p.path}"
        except ValueError:
            return "<url>"
    return str(value)


def _failure_classification(exc: BaseException, *, detail: str = "") -> dict[str, str]:
    """Bounded, non-sensitive failure classification for refinement feedback.

    Returns the exception type name and a truncated, redacted message. Never
    includes paths, URLs, secrets, or full tracebacks. ``detail`` carries an
    optional bounded hint (input format, parser stderr tail) that helps the
    authoring model self-correct.
    """
    message = str(exc)
    if detail:
        message = f"{message} | {detail}"
    # Redact anything that looks like a path or URL.
    message = re.sub(r"(?:https?://|[A-Za-z]:[\\/]|\\\\|/(?:tmp|home|Users|var|private)/)[^\s\"<>]*", "<path>", message)
    return {
        "error_type": type(exc).__name__,
        "error_message": message[:500],
    }


def _stop_reason(run_result: dict) -> str:
    """Why the runtime stopped the parser, in words a rewrite can act on (round 9 retest, A042).

    A parser stopped by the time limit leaves no traceback, so before this the feedback said only
    "parser failed" and every rewrite repeated the same slow reading.
    """
    from .runtime import Limits
    seconds = int(Limits().timeout_seconds)
    failure, exit_code = run_result.get("failure"), run_result.get("exit_code")
    if failure == "timeout" or (failure == "parser_exit" and isinstance(exit_code, int) and exit_code < 0
                                and not run_result.get("stderr_tail")):
        return (f"the parser was stopped at the {seconds}-second time limit; read each sheet once "
                "(for example iter_rows(values_only=True)) and never call worksheet.cell in loops over every row and column")
    return {"log_limit": "the parser wrote more than the log limit to stdout/stderr",
            "output_limit": "the parser output exceeded the output size limit",
            "row_map_limit": "the row map exceeded its size limit",
            "invalid_output": "the parser exited without writing the output workbook"}.get(str(failure), "")


def _input_format_hint(input_bytes: bytes, input_name: str) -> str:
    """Bounded format hint derived from the input's magic bytes.

    Helps the authoring model pick the right reader (e.g. xlrd for legacy
    binary .xls files, which openpyxl cannot open).
    """
    head = input_bytes[:8]
    if head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return ("input is a legacy binary Excel file (OLE2/BIFF .xls); "
                "openpyxl cannot read it - use xlrd (open_workbook(file_contents=...)) instead")
    if head.startswith(b"PK\x03\x04"):
        return "input is a ZIP-based Office file (.xlsx); use openpyxl"
    if head.startswith(b"<?xml") or head.lstrip().startswith(b"<"):
        return "input appears to be XML/HTML text, not a binary spreadsheet"
    return ""


class UrllibTransport:
    def get(self, url: str, *, max_bytes: int, timeout: float) -> bytes:
        with urlopen(Request(_url(url), method="GET"), timeout=timeout) as response:
            data = response.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise ValueError("transfer byte limit")
        return data

    def put(self, url: str, body: bytes, *, headers: dict[str, str], timeout: float) -> int:
        request = Request(_url(url), data=body, headers=headers, method="PUT")
        try:
            with urlopen(request, timeout=timeout) as response:
                return int(response.status)
        except HTTPError as exc:
            if exc.code in (409, 412):
                raise RuntimeError("output already exists") from None
            raise RuntimeError("transfer failed") from None
        except (URLError, socket.timeout, OSError):
            raise RuntimeError("transfer failed") from None


def _expected(env: dict[str, str], name: str, size: bool = False) -> int | str:
    key = name + ("_SIZE" if size else "_SHA256")
    value = env[key]
    if size:
        try:
            result = int(value)
            if result < 0: raise ValueError
            return result
        except ValueError:
            raise ValueError("invalid expected size") from None
    if len(value) != 64 or any(c not in "0123456789abcdefABCDEF" for c in value):
        raise ValueError("invalid expected hash")
    return value.lower()


def _evidence(data: bytes, expected: str, size: int) -> dict[str, object]:
    return {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data),
            "expected_sha256": expected, "expected_size": size,
            "verified": len(data) == size and hashlib.sha256(data).hexdigest() == expected}


def remote_job(env: dict[str, str] | None = None, *, transport=None, run: Callable = None,
               timeout: float = 30.0) -> tuple[dict, int]:
    env = dict(os.environ if env is None else env)
    if not all(env.get(k) for k in REQUIRED):
        raise ValueError("remote mode requires complete transfer configuration")
    unknown = [k for k in env if k.startswith("WORKER_") and k not in ALLOWED_WORKER]
    if unknown:
        raise ValueError("unknown worker configuration")
    output_name = env["WORKER_OUTPUT_NAME"]
    content_type = env["WORKER_OUTPUT_CONTENT_TYPE"]
    if OUTPUT_CONTENT_TYPES.get(output_name) != content_type:
        raise ValueError("invalid worker output policy")
    urls = {k: _url(env[k]) for k in ("INPUT_URL", "PARSER_URL", "OUTPUT_URL", "RESULT_URL")}
    interpretation_enabled = bool(env.get("WORKER_INTERPRETATION_NAME") or env.get("INTERPRETATION_URL"))
    if interpretation_enabled:
        if env.get("WORKER_INTERPRETATION_NAME") != "interpretation.json" or not env.get("INTERPRETATION_URL"):
            raise ValueError("incomplete interpretation configuration")
        urls["INTERPRETATION_URL"] = _url(env["INTERPRETATION_URL"])
    row_map_enabled = bool(env.get("WORKER_ROW_MAP_NAME") or env.get("ROW_MAP_URL"))
    if row_map_enabled:
        if (env.get("WORKER_ROW_MAP_NAME") != "row-map.json" or not env.get("ROW_MAP_URL")
                or interpretation_enabled):
            raise ValueError("incomplete row map configuration")
        urls["ROW_MAP_URL"] = _url(env["ROW_MAP_URL"])
    input_size = _expected(env, "INPUT", True); parser_size = _expected(env, "PARSER", True)
    input_hash = _expected(env, "INPUT"); parser_hash = _expected(env, "PARSER")
    transport = transport or UrllibTransport()
    run = run or __import__("worker.runtime", fromlist=["run_job"]).run_job
    result: dict = {"contract_version": "remote-worker.v1", "job": env["WORKER_JOB_ID"], "status": "failed",
                    "failure": None, "input": {}, "parser": {},
                    "output": {"sha256": None, "size": 0, "content_type": content_type}}
    if interpretation_enabled:
        result["contract_version"] = "remote-worker.v2"
    if row_map_enabled:
        result["contract_version"] = "remote-worker.v3"
    try:
        parser = transport.get(urls["PARSER_URL"], max_bytes=parser_size, timeout=timeout)
        source = transport.get(urls["INPUT_URL"], max_bytes=input_size, timeout=timeout)
        result["parser"] = _evidence(parser, parser_hash, parser_size)
        result["input"] = _evidence(source, input_hash, input_size)
        if not result["parser"]["verified"] or not result["input"]["verified"]:
            raise ValueError("transfer integrity check failed")
        input_name = env["WORKER_INPUT_NAME"]
        if input_name not in ("input.csv", "input.xls", "input.xlsx"):
            raise ValueError("invalid input name")
        options = ({"interpretation_name": "interpretation.json"} if interpretation_enabled
                   else {"row_map_name": "row-map.json"} if row_map_enabled else {})
        ran = run(job=env["WORKER_JOB_ID"], parser_bytes=parser, input_bytes=source,
                  input_name=input_name, output_name=output_name,
                  output_policy="opaque", return_output=True, **options)
        run_result, code, output = ran[:3]
        run_result.pop("logs", None)
        if code != 0 or output is None:
            stderr_tail = str(run_result.get("stderr_tail") or "")
            format_hint = _input_format_hint(source, input_name)
            detail = " ".join(part for part in (_stop_reason(run_result), format_hint, stderr_tail) if part)
            raise RuntimeError(f"parser failed{(' | ' + detail) if detail else ''}")
        sidecar = None
        if interpretation_enabled:
            if len(ran) != 4 or not isinstance(ran[3], bytes) or not 0 < len(ran[3]) <= 1024 * 1024:
                raise ValueError("interpretation artifact missing")
            sidecar = ran[3]
            from bom_processor.interpretation_coverage import verify_coverage
            verification = verify_coverage(source, os.path.splitext(input_name)[1], output, sidecar)
            result["interpretation"] = {"sha256": hashlib.sha256(sidecar).hexdigest(), "size": len(sidecar),
                                        "content_type": "application/json", "verification": verification}
        mapped = None
        if row_map_enabled:
            from bom_processor.row_accounting import MAX_ROW_MAP_BYTES
            if len(ran) != 4 or not isinstance(ran[3], bytes) or not 0 < len(ran[3]) <= MAX_ROW_MAP_BYTES:
                raise ValueError("row map artifact missing")
            mapped = ran[3]
            # Verified by candidate validation against the pinned source; only bound here.
            result["row_map"] = {"sha256": hashlib.sha256(mapped).hexdigest(), "size": len(mapped),
                                 "content_type": "application/json"}
        result["output"] = {"sha256": hashlib.sha256(output).hexdigest(), "size": len(output),
                     "content_type": content_type}
        result["parser"] = _evidence(parser, parser_hash, parser_size)
        result["input"] = _evidence(source, input_hash, input_size)
        headers = {"x-ms-blob-type": "BlockBlob", "If-None-Match": "*", "Content-Type": content_type, "Content-Length": str(len(output))}
        if transport.put(urls["OUTPUT_URL"], output, headers=headers, timeout=timeout) != 201:
            raise RuntimeError("output transfer failed")
        if sidecar is not None:
            if transport.put(urls["INTERPRETATION_URL"], sidecar,
                             headers={"x-ms-blob-type": "BlockBlob", "If-None-Match": "*",
                                      "Content-Type": "application/json", "Content-Length": str(len(sidecar))},
                             timeout=timeout) != 201:
                raise RuntimeError("interpretation transfer failed")
        if mapped is not None:
            if transport.put(urls["ROW_MAP_URL"], mapped,
                             headers={"x-ms-blob-type": "BlockBlob", "If-None-Match": "*",
                                      "Content-Type": "application/json", "Content-Length": str(len(mapped))},
                             timeout=timeout) != 201:
                raise RuntimeError("row map transfer failed")
        result["status"] = "completed"; result["failure"] = None
        body = json.dumps(result, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode()
        result_headers = {"x-ms-blob-type": "BlockBlob", "If-None-Match": "*", "Content-Type": "application/json", "Content-Length": str(len(body))}
        if transport.put(urls["RESULT_URL"], body, headers=result_headers, timeout=timeout) != 201:
            raise RuntimeError("result transfer failed")
        return result, 0
    except Exception as exc:
        result["status"] = "failed"; result["failure"] = "transfer_failed"
        result["failure_classification"] = _failure_classification(exc)
        if isinstance(exc, RuntimeError) and "already exists" in str(exc):
            result["failure"] = "conflict"
        try:
            body = json.dumps(result, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode()
            transport.put(urls["RESULT_URL"], body, headers={"x-ms-blob-type": "BlockBlob", "If-None-Match": "*", "Content-Type": "application/json", "Content-Length": str(len(body))}, timeout=timeout)
        except Exception:
            pass
        return result, 3