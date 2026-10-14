"""Bounded parser execution runtime for synthetic and opaque outputs."""
from __future__ import annotations

import hashlib
import json
import os
import platform
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

CONTRACT_VERSION = "synthetic-worker.v1"


@dataclass(frozen=True)
class Limits:
    parser_bytes: int = 256 * 1024
    input_bytes: int = 1024 * 1024
    output_bytes: int = 1024 * 1024
    log_bytes: int = 64 * 1024
    timeout_seconds: float = 5.0


DEFAULT_FIXTURES = Path(__file__).resolve().parent / "fixtures"
OUTPUT_POLICIES = {"synthetic_json", "opaque"}


def _safe_output_name(name: str) -> bool:
    path = Path(name)
    return (bool(name) and "\x00" not in name and "\\" not in name and
            path.name == name and name not in {".", ".."} and not path.is_absolute())


def _digest(path: Path) -> tuple[str, int]:
    data = path.read_bytes()
    return hashlib.sha256(data).hexdigest(), len(data)


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _regular(path: Path, root: Path) -> bool:
    return _inside(path, root) and path.is_file() and not path.is_symlink()


def _bounded_log_tail(path: Path, limits: Limits, max_chars: int = 500) -> str:
    """Return a bounded, path-redacted tail of a log file for failure feedback.

    Only the last ``max_chars`` characters are kept so a noisy parser cannot
    bloat the result. Temp-directory paths are redacted to avoid leaking the
    worker's filesystem layout.
    """
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    text = data[-(max_chars * 4):].decode("utf-8", errors="replace")
    import re as _re
    text = _re.sub(r"(?:/[^\s\"'<>]*agentquote[^\s\"'<>]*|[A-Za-z]:[\\/][^\s\"'<>]*)", "<path>", text)
    return text[-max_chars:].strip()


def _preexec(limits: Limits) -> Callable[[], None] | None:
    if os.name != "posix":
        return None
    def apply() -> None:
        try:
            import resource
            resource.setrlimit(resource.RLIMIT_FSIZE, (limits.output_bytes, limits.output_bytes))
            resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))
            resource.setrlimit(resource.RLIMIT_NPROC, (16, 16))
            resource.setrlimit(resource.RLIMIT_CPU, (max(1, int(limits.timeout_seconds)), max(1, int(limits.timeout_seconds) + 1)))
        except (AttributeError, OSError, ValueError):
            pass
    return apply


def _result(job: str, status: str, failure: str | None, **extra: object) -> dict:
    return {"contract_version": CONTRACT_VERSION, "job": job, "status": status,
            "failure": failure, **extra}


def _terminate_tree(proc: subprocess.Popen) -> None:
    """Terminate the parser and descendants before temporary files are removed."""
    if os.name == "posix":
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=0.3)
            return
        except Exception:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    elif os.name == "nt":
        # taskkill is the standard Windows process-tree cleanup mechanism.
        subprocess.run(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        proc.kill()
    try:
        proc.wait(timeout=1)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def run_job(*, job: str = "synthetic", parser_bytes: bytes | None = None,
            input_bytes: bytes | None = None, limits: Limits | None = None,
    return_output: bool = False, input_name: str = "input.csv",
    output_name: str = "output.json",
        output_policy: str = "synthetic_json", interpretation_name: str | None = None,
        row_map_name: str | None = None):
    """Run an approved parser with either deterministic JSON or opaque output.

    With row_map_name (plan 8.8 step 4a) the parser also gets --row-map and must write it; the
    bytes are returned as a fourth value and verified by candidate validation, not here.
    """
    limits = limits or Limits()
    if interpretation_name is not None and (interpretation_name != "interpretation.json"
                                            or not return_output or output_policy != "opaque"
                                            or interpretation_name in (input_name, output_name)):
        raise ValueError("invalid interpretation configuration")
    if row_map_name is not None and (row_map_name != "row-map.json" or interpretation_name is not None
                                     or not return_output or output_policy != "opaque"
                                     or row_map_name in (input_name, output_name)):
        raise ValueError("invalid row map configuration")
    from bom_processor.row_accounting import MAX_ROW_MAP_BYTES
    parser_bytes = parser_bytes if parser_bytes is not None else (DEFAULT_FIXTURES / "synthetic_parser.py").read_bytes()
    input_bytes = input_bytes if input_bytes is not None else (DEFAULT_FIXTURES / "synthetic_input.csv").read_bytes()
    started = time.monotonic()
    base = {"parser": {"sha256": hashlib.sha256(parser_bytes).hexdigest(), "size": len(parser_bytes)},
            "input": {"sha256": hashlib.sha256(input_bytes).hexdigest(), "size": len(input_bytes)},
            "output": {"sha256": None, "size": 0}, "limits": asdict(limits),
            "logs": {"stdout": {"size": 0}, "stderr": {"size": 0}}}
    if output_policy not in OUTPUT_POLICIES or not _safe_output_name(input_name) or not _safe_output_name(output_name):
        value = (_result(job, "failed", "invalid_output", **base), 5)
        return (*value, None) if return_output else value
    if output_policy == "synthetic_json" and output_name != "output.json":
        value = (_result(job, "failed", "invalid_output", **base), 5)
        return (*value, None) if return_output else value
    if len(parser_bytes) > limits.parser_bytes or len(input_bytes) > limits.input_bytes:
        value = (_result(job, "failed", "input_limit", **base), 2)
        return (*value, None) if return_output else value
    with tempfile.TemporaryDirectory(prefix="agentquote-worker-") as temp:
        root = Path(temp).resolve()
        parser = root / "parser.py"; source = root / input_name; output = root / output_name
        interpretation = root / interpretation_name if interpretation_name else None
        row_map = root / row_map_name if row_map_name else None
        stdout = root / "stdout.log"; stderr = root / "stderr.log"
        parser.write_bytes(parser_bytes); source.write_bytes(input_bytes)
        for path in (parser, source):
            if not _regular(path, root):
                value = (_result(job, "failed", "unsafe_fixture", **base), 2)
                return (*value, None) if return_output else value
        env = {"PATH": os.defpath, "LANG": "C", "LC_ALL": "C", "HOME": str(root / "home"),
               "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1"}
        if os.name == "nt":
            # Windows Python needs these loader hints; they are not credentials.
            for name in ("SystemRoot", "WINDIR"):
                if name in os.environ:
                    env[name] = os.environ[name]
        (root / "home").mkdir()
        proc = None
        try:
            with stdout.open("wb") as out, stderr.open("wb") as err:
                kwargs = {"shell": False, "cwd": str(root), "stdin": subprocess.DEVNULL,
                          "stdout": out, "stderr": err, "env": env, "preexec_fn": _preexec(limits)}
                if os.name == "posix":
                    kwargs["start_new_session"] = True
                elif os.name == "nt":
                    kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
                command = [sys.executable, str(parser), "--input", str(source), "--output", str(output)]
                if interpretation is not None:
                    command.extend(["--interpretation", str(interpretation)])
                if row_map is not None:
                    command.extend(["--row-map", str(row_map)])
                proc = subprocess.Popen(command, **kwargs)
                failure = None
                while proc.poll() is None:
                    if time.monotonic() - started > limits.timeout_seconds:
                        failure = "timeout"; break
                    if stdout.stat().st_size > limits.log_bytes or stderr.stat().st_size > limits.log_bytes:
                        failure = "log_limit"; break
                    if output.exists() and output.stat().st_size > limits.output_bytes:
                        failure = "output_limit"; break
                    if interpretation is not None and interpretation.exists() and interpretation.stat().st_size > 1024 * 1024:
                        failure = "interpretation_limit"; break
                    if row_map is not None and row_map.exists() and row_map.stat().st_size > MAX_ROW_MAP_BYTES:
                        failure = "row_map_limit"; break
                    time.sleep(0.01)
                if failure:
                    _terminate_tree(proc)
                else:
                    proc.wait()
            for name, path in (("stdout", stdout), ("stderr", stderr)):
                base["logs"][name] = {"size": path.stat().st_size, "sha256": _digest(path)[0]}
            if failure is None and any(base["logs"][name]["size"] > limits.log_bytes for name in ("stdout", "stderr")):
                failure = "log_limit"
            if failure:
                value = (_result(job, "failed", failure, **base), 3)
                return (*value, None) if return_output else value
            if proc.returncode != 0:
                base["stderr_tail"] = _bounded_log_tail(stderr, limits)
                value = (_result(job, "failed", "parser_exit", exit_code=proc.returncode, **base), 4)
                return (*value, None) if return_output else value
            if not _regular(output, root) or output.stat().st_size == 0:
                value = (_result(job, "failed", "invalid_output", exit_code=proc.returncode, **base), 5)
                return (*value, None) if return_output else value
            if output.stat().st_size > limits.output_bytes:
                value = (_result(job, "failed", "output_limit", exit_code=proc.returncode, **base), 3)
                return (*value, None) if return_output else value
            raw = output.read_bytes()
            if output_policy == "synthetic_json":
                value = json.loads(raw.decode("utf-8"))
                if not isinstance(value, dict) or value.get("schema") != "agentquote.synthetic.v1" or not isinstance(value.get("rows"), list):
                    raise ValueError("schema")
                canonical = json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode()
                if raw != canonical:
                    raise ValueError("nondeterministic")
            base["output"] = {"sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw)}
            if interpretation is not None:
                if not _regular(interpretation, root) or not 0 < interpretation.stat().st_size <= 1024 * 1024:
                    return _result(job, "failed", "invalid_interpretation", **base), 5, None
                sidecar = interpretation.read_bytes()
                from bom_processor.interpretation_coverage import verify_coverage
                try:
                    verification = verify_coverage(input_bytes, Path(input_name).suffix, raw, sidecar)
                except ValueError:
                    return _result(job, "failed", "interpretation_verification_failed", **base), 5, None
                base["interpretation"] = {"sha256": hashlib.sha256(sidecar).hexdigest(), "size": len(sidecar),
                                          "verification": verification}
                return _result(job, "completed", None, exit_code=0, **base), 0, raw, sidecar
            if row_map is not None:
                if not _regular(row_map, root) or not 0 < row_map.stat().st_size <= MAX_ROW_MAP_BYTES:
                    return _result(job, "failed", "invalid_row_map", **base), 5, None
                mapped = row_map.read_bytes()
                base["row_map"] = {"sha256": hashlib.sha256(mapped).hexdigest(), "size": len(mapped)}
                return _result(job, "completed", None, exit_code=0, **base), 0, raw, mapped
            value = (_result(job, "completed", None, exit_code=0, **base), 0, raw)
            return value if return_output else value[:2]
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
            value = (_result(job, "failed", "invalid_output", **base), 5)
            return (*value, None) if return_output else value
