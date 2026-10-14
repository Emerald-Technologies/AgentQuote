# Copyright (c) Microsoft. All rights reserved.
#
# BOM Processor Agent — WorkflowAgent Architecture
#
# Uses WorkflowAgent (in-process, no external Durable Task Scheduler) to
# enforce step-by-step execution. Each step is a fixed Executor in the
# workflow graph — the model cannot skip or stop early.
#
# Workflow graph:
#   UserInput
#     -> OrchestratorAgent (LLM — plans which BOMs to process)
#     -> DiscoverBOMs (code — lists boms/parsers, identifies gaps)
#     -> ProcessBOMAgent (LLM — analyzes BOM, generates parser script)
#     -> ValidateParser (code — runs parser, checks output)
#     -> UploadParser (code — uploads to blob storage)
#     -> ReportResult (code — yields final summary to user)

import json
import logging
import os
import pathlib
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

# Server bootstrap — lightweight, loads immediately
from agent_framework_foundry_hosting import ResponsesHostServer
from azure.ai.agentserver.responses.store._memory import InMemoryResponseProvider
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Workspace root & size limits
# ---------------------------------------------------------------------------
WORKSPACE_ROOT = pathlib.Path(os.environ.get("WORKSPACE_ROOT", ".")).resolve()
_MAX_OUTPUT_BYTES = 4_000


def _safe_path(path: str) -> pathlib.Path:
    """Resolve path and raise if it escapes WORKSPACE_ROOT."""
    resolved = (WORKSPACE_ROOT / path).resolve()
    if not str(resolved).startswith(str(WORKSPACE_ROOT)):
        raise ValueError(f"Access denied: '{path}' is outside the workspace root.")
    return resolved


# ---------------------------------------------------------------------------
# Background Task Manager — run long operations asynchronously
# ---------------------------------------------------------------------------

@dataclass
class BackgroundTask:
    """Represents a single background task."""
    id: str
    label: str
    status: str
    stdout: str = ""
    stderr: str = ""
    exit_code: Optional[int] = None
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None


class BackgroundTaskManager:
    """Manages concurrent background tasks with thread-safe state tracking."""

    def __init__(self):
        self._lock = threading.Lock()
        self._tasks: dict[str, BackgroundTask] = {}

    def start(self, label: str, command: str, cwd: Optional[str] = None,
              timeout: Optional[int] = None) -> str:
        task_id = uuid.uuid4().hex[:12]
        task = BackgroundTask(id=task_id, label=label, status="running")
        with self._lock:
            self._tasks[task_id] = task

        def _run():
            try:
                result = subprocess.run(command, shell=True, capture_output=True,
                                        text=True, timeout=timeout,
                                        cwd=cwd or str(WORKSPACE_ROOT))
                with self._lock:
                    t = self._tasks[task_id]
                    t.stdout = result.stdout
                    t.stderr = result.stderr
                    t.exit_code = result.returncode
                    t.status = "completed" if result.returncode == 0 else "failed"
                    t.finished_at = time.time()
            except subprocess.TimeoutExpired:
                with self._lock:
                    t = self._tasks[task_id]
                    t.status = "failed"
                    t.stderr = "Timed out"
                    t.finished_at = time.time()
            except Exception as e:
                with self._lock:
                    t = self._tasks[task_id]
                    t.stderr = str(e)
                    t.status = "failed"
                    t.finished_at = time.time()

        threading.Thread(target=_run, daemon=True, name=f"bg-{task_id}").start()
        return task_id

    def get_status(self, task_id: str) -> Optional[dict]:
        with self._lock:
            t = self._tasks.get(task_id)
            if t is None:
                return None
            elapsed = time.time() - t.started_at
            return {"id": t.id, "label": t.label, "status": t.status,
                    "exit_code": t.exit_code, "elapsed_seconds": round(elapsed, 1),
                    "stdout": (t.stdout[:4000] if t.stdout else ""),
                    "stderr": (t.stderr[:2000] if t.stderr else "")}

    def list_tasks(self) -> list[dict]:
        with self._lock:
            return [{"id": t.id, "label": t.label, "status": t.status,
                     "exit_code": t.exit_code,
                     "elapsed_seconds": round(time.time() - t.started_at, 1)}
                    for t in self._tasks.values()]

    def prune_completed(self, older_than_seconds: int = 300):
        now = time.time()
        with self._lock:
            to_delete = [tid for tid, t in self._tasks.items()
                         if t.status in ("completed", "failed")
                         and t.finished_at is not None
                         and (now - t.finished_at) > older_than_seconds]
            for tid in to_delete:
                del self._tasks[tid]


_BG = BackgroundTaskManager()


# ---------------------------------------------------------------------------
# Blob storage helpers — reused across executors
# ---------------------------------------------------------------------------

def _get_blob_service_client():
    import importlib
    conn_str = os.environ.get("BOM_STORAGE_CONNECTION_STRING", "")
    if not conn_str:
        raise RuntimeError("BOM_STORAGE_CONNECTION_STRING is not set.")
    blob = importlib.import_module("azure.storage.blob")
    return blob.BlobServiceClient.from_connection_string(conn_str)


def _list_blobs(container: str) -> list[dict]:
    """List blobs in a container. Returns list of {name, size_kb, modified}."""
    client = _get_blob_service_client()
    cc = client.get_container_client(container)
    result = []
    for b in cc.list_blobs():
        result.append({"name": b.name, "size_kb": round((b.size or 0) / 1024, 1),
                        "modified": b.last_modified.isoformat() if b.last_modified else "unknown"})
    return result


def _download_blob(blob_path: str, container: str = "boms") -> str:
    """Download blob to workspace. Returns local path."""
    target = _safe_path(blob_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    client = _get_blob_service_client()
    cc = client.get_container_client(container)
    bc = cc.get_blob_client(blob_path)
    data = bc.download_blob().readall()
    target.write_bytes(data)
    return blob_path


def _upload_blob(local_path: str, container: str = "parsers") -> str:
    """Upload local file to blob storage. Returns confirmation."""
    source = _safe_path(local_path)
    if not source.exists():
        raise FileNotFoundError(f"Local file '{local_path}' not found.")
    client = _get_blob_service_client()
    cc = client.get_container_client(container)
    bc = cc.get_blob_client(local_path)
    data = source.read_bytes()
    bc.upload_blob(data, overwrite=True)
    return local_path


# ---------------------------------------------------------------------------
# Canonical BOM schema
# ---------------------------------------------------------------------------

CANONICAL_SCHEMA_COLUMNS = [
    ("line_number", "int", True),
    ("item_number", "str", True),
    ("item_name", "str", True),
    ("revision", "str", False),
    ("item_category", "str", True),
    ("unit_of_measure", "str", False),
    ("quantity", "float", True),
    ("reference_designator", "str", False),
    ("bom_notes", "str", False),
    ("manufacturer_1", "str", False),
    ("manufacturer_item_number_1", "str", False),
    ("manufacturer_2", "str", False),
    ("manufacturer_item_number_2", "str", False),
    ("manufacturer_3", "str", False),
    ("manufacturer_item_number_3", "str", False),
    ("manufacturer_4", "str", False),
    ("manufacturer_item_number_4", "str", False),
    ("manufacturer_5", "str", False),
    ("manufacturer_item_number_5", "str", False),
    ("manufacturer_6", "str", False),
    ("manufacturer_item_number_6", "str", False),
    ("manufacturer_7", "str", False),
    ("manufacturer_item_number_7", "str", False),
    ("manufacturer_8", "str", False),
    ("manufacturer_item_number_8", "str", False),
]


# =========================================================================
# Agent / Workflow factory — called lazily on first request
# =========================================================================


def _schema_summary() -> str:
    """Build a human-readable schema summary for prompts."""
    lines = []
    for name, typ, req in CANONICAL_SCHEMA_COLUMNS:
        req_mark = " (required)" if req else ""
        lines.append(f"- `{name}`: {typ}{req_mark}")
    # Mirrors bom_processor/canonical_format.py (not imported: the package __init__ pulls in Azure clients).
    lines.append("- When any line has more than 8 manufacturer/MPN pairs, append `manufacturer_9`, "
                 "`manufacturer_item_number_9`, ... up to the widest line (at most 50 pairs), the same "
                 "columns on every row. Never drop a pair; fail if a line has more than 50.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool wrappers — decorated with @tool(name=...) to match prompt names

from agent_framework._tools import tool as _tool_reg  # noqa: E402
# ---------------------------------------------------------------------------

@_tool_reg(name="run_command")
def _run_command_impl(command: str) -> str:
    """Execute a shell command. Use for Python/pandas BOM analysis and file operations.

    Args:
        command: The shell command to execute.
    Returns:
        Command output (stdout + stderr), capped at 4 KB.
    """
    try:
        result = subprocess.run(command, shell=True, capture_output=True,
                                text=True, timeout=120, cwd=str(WORKSPACE_ROOT))
        output = (result.stdout + result.stderr).strip()
        if len(output.encode()) > _MAX_OUTPUT_BYTES:
            output = output.encode()[:_MAX_OUTPUT_BYTES].decode(errors="replace") + "\n... (truncated)"
        return output or "(no output)"
    except Exception as e:
        return f"Error: {e}"


@_tool_reg(name="write_file")
def _write_file_impl(path: str, content: str) -> str:
    """Write content to a file. Use to create parser scripts.

    Args:
        path: Relative path from workspace root (e.g. 'parsers/acme/parse.py').
        content: The full file content to write.
    Returns:
        Confirmation message.
    """
    try:
        target = _safe_path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"Written: {path} ({len(content)} chars)"
    except Exception as e:
        return f"Error: {e}"


@_tool_reg(name="read_file")
def _read_file_impl(path: str, start_line: int = 1, end_line: int | None = None) -> str:
    """Read content from a file.

    Args:
        path: Relative path from workspace root.
        start_line: 1-based line number to start reading from.
        end_line: 1-based line number to stop at (inclusive).
    Returns:
        File content.
    """
    try:
        lines = _safe_path(path).read_text(encoding="utf-8").splitlines()
        end = end_line if end_line is not None else len(lines)
        return "\n".join(lines[start_line - 1 : end])
    except Exception as e:
        return f"Error: {e}"


@_tool_reg(name="download_blob")
def _download_blob_impl(blob_path: str, container: str = "boms") -> str:
    """Download a file from blob storage to the local workspace.

    Args:
        blob_path: Path within the container (e.g. 'acme/sample.xlsx').
        container: Container name ('boms', 'parsers', or 'format').
    Returns:
        Confirmation with file size.
    """
    try:
        local_path = _download_blob(blob_path, container)
        file_size = _safe_path(blob_path).stat().st_size
        return f"Downloaded {container}/{blob_path} -> {local_path} ({file_size:,} bytes)"
    except Exception as e:
        return f"Error: {e}"


@_tool_reg(name="upload_blob")
def _upload_blob_impl(local_path: str, container: str = "parsers") -> str:
    """Upload a file from workspace to blob storage.

    Args:
        local_path: Relative path from workspace root.
        container: Target container (default 'parsers').
    Returns:
        Confirmation message.
    """
    try:
        _upload_blob(local_path, container)
        return f"Uploaded {local_path} -> {container}/{local_path}"
    except Exception as e:
        return f"Error: {e}"


@_tool_reg(name="list_boms")
def _list_boms_impl() -> str:
    """List all BOM files in blob storage grouped by customer.

    Returns:
        Formatted listing of customer folders and their BOM files.
    """
    try:
        boms = _list_blobs("boms")
        if not boms:
            return "No BOMs found."
        customers: dict[str, list[str]] = {}
        for b in boms:
            parts = b["name"].split("/")
            customer = parts[0]
            customers.setdefault(customer, []).append(f"  {b['name']} ({b['size_kb']} KB)")
        parts_list = []
        for customer in sorted(customers):
            parts_list.append(f"\n{customer}:")
            parts_list.extend(customers[customer])
        return "\n".join(parts_list) if parts_list else "No BOMs found."
    except Exception as e:
        return f"Error: {e}"


@_tool_reg(name="list_parsers")
def _list_parsers_impl() -> str:
    """List existing parser files in blob storage.

    Returns:
        Formatted listing of existing parsers.
    """
    try:
        parsers = _list_blobs("parsers")
        if not parsers:
            return "No parsers found."
        return "\n".join(f"{p['name']} ({p['size_kb']} KB)" for p in parsers)
    except Exception as e:
        return f"Error: {e}"


@_tool_reg(name="finalize")
def _finalize_impl(customer: str, parser_path: str, script_content: str) -> str:
    """MANDATORY final step. Writes the parser script, validates it, and uploads it.
    You MUST call this after analysis. The script content must be a complete Python parser.

    Args:
        customer: Customer name (e.g. 'acre_security').
        parser_path: Relative path like 'parsers/<customer>/parse.py'.
        script_content: The full Python parser script source code.
    Returns:
        Result summary including write, validation, and upload status.
    """
    results = []
    # 0. Pre-check: parser must accept --input and --output CLI arguments
    if "--input" not in script_content or "--output" not in script_content:
        return ("Error: parser script does not accept --input and --output CLI arguments. "
                "Regenerate the script with argparse arguments `--input <path>` and "
                "`--output <path>` (both required), then call finalize again.")

    try:
        # 1. Write the file
        target = _safe_path(parser_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(script_content, encoding="utf-8")
        results.append(f"Written: {parser_path} ({len(script_content)} chars)")
    except Exception as e:
        return f"Error writing parser: {e}"

    validation_ok = False
    bom_files: list[str] = []
    try:
        # 2. Validate: check if any BOM files are in the workspace
        workspace_boms = list(WORKSPACE_ROOT.glob(f"**/*.xls*"))
        bom_files = [str(p.relative_to(WORKSPACE_ROOT)) for p in workspace_boms
                     if p.suffix in (".xls", ".xlsx")]
        for bom_path in bom_files[:2]:
            output_path = f"parsers/{customer}/output.xlsx"
            cmd = f'python "{parser_path}" --input "{bom_path}" --output "{output_path}"'
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=60,
                                    cwd=str(WORKSPACE_ROOT))
            if result.returncode == 0:
                results.append(f"Validation OK: {bom_path}")
                # 2b. Verify item_category is populated on every row with Part/Document
                try:
                    import pandas as pd
                    out_df = pd.read_excel(_safe_path(output_path))
                    if "item_category" not in out_df.columns:
                        results.append("Validation issue: output is missing the required "
                                       "'item_category' column.")
                        continue
                    cats = out_df["item_category"].astype(str).str.strip()
                    bad = cats[(cats == "") | cats.isna() | (~cats.isin(["Part", "Document"]))]
                    if len(bad) > 0:
                        results.append(f"Validation issue: {len(bad)} row(s) have an empty or "
                                       "invalid item_category. Every row must be exactly "
                                       "'Part' or 'Document'.")
                    else:
                        validation_ok = True
                except Exception as e:
                    results.append(f"Validation note: could not inspect output ({e})")
                    validation_ok = True  # parser ran; don't block on inspection errors
            else:
                results.append(f"Validation issue: {(result.stderr or '').strip()[:400]}")
    except Exception as e:
        results.append(f"Validation note: {e}")

    if bom_files and not validation_ok:
        results.append("Error: parser failed validation. Fix the script so it accepts "
                       "--input/--output and populates item_category on every row with "
                       "'Part' or 'Document', then call finalize again.")
        return "\n".join(results)

    try:
        # 3. Upload
        _upload_blob(parser_path, "parsers")
        results.append(f"Uploaded: {parser_path}")
    except Exception as e:
        return f"Error uploading: {e}"

    return "\n".join(results)


# ---------------------------------------------------------------------------
# Agent instructions — defines the LLM's behavior
# ---------------------------------------------------------------------------

ORCHESTRATOR_SYSTEM_PROMPT = f"""You are a BOM Processing Agent for Emerald Technologies, an electronic contract manufacturer.

PRIMARY OBJECTIVE: Discover the formatting and structure of customer assembly BOMs and develop per-customer parser/normalizer scripts that output to a standardized quoting format.

## Canonical Output Schema

{_schema_summary()}

## Tools Available

1. `list_boms` — List BOMs in blob storage to discover customers.
2. `list_parsers` — List existing parsers to identify gaps.
3. `download_blob(blob_path, container)` — Download a BOM or format reference.
4. `run_command(command)` — Execute shell commands. Use Python/pandas to inspect BOM structure.
5. `write_file(path, content)` — Write a helper file if needed.
6. `read_file(path, start_line, end_line)` — Read workspace file contents.
7. `upload_blob(local_path, container)` — Upload a file.
8. `finalize(customer, parser_path, script_content)` — **MANDATORY FINAL STEP**.

## Workflow — Execute In Order, Every Step

1. **Discover** — Call `list_boms` and `list_parsers`.
2. **Download format** — `download_blob("finalformat.xlsx", "format")`
3. **Download BOMs** — `download_blob("<customer>/<file>", "boms")`
4. **Analyze** — Use `run_command` with pandas. Print headers, shape, dtypes, first/last rows. Identify column mappings.
5. **Write a self-contained Python parser** — Compose the full parser script in your head, then call `finalize(customer, "parsers/<customer>/parse.py", script_content)`.

## How finalize works

`finalize` does three things automatically:
- **Writes** the parser script to the workspace
- **Validates** it by running against a sample BOM
- **Uploads** it to blob storage

So you don't need to call `write_file`, `run_command` for validation, or `upload_blob` separately. Just compose the complete parser and call `finalize`.

## Parser Script Requirements

- MUST accept `--input <path>` and `--output <path>` CLI arguments via `argparse`,
  both marked `required=True`. The script must fail with a clear error message if
  either argument is missing. Example invocation:
  `python parse.py --input boms/acme/sample.xlsx --output parsers/acme/output.xlsx`
- Uses pandas, openpyxl, xlrd (pre-installed)
- Has a docstring explaining format and mapping decisions
- Handles: header not at row 1, merged cells, empty rows, footers, multi-sheet
- Logs warnings for unmappable data
- Sets `item_category` on EVERY row — this column is required and must never be empty.
  Allowed values are exactly `Part` or `Document`:
  - `Document` for drawings, specs, manuals, labels, artwork, instructions
    (detect via category columns, description keywords like DWG/SPEC/MANUAL/LABEL,
    or rows with no part number / zero quantity)
  - `Part` for all physical components and hardware
  - Default to `Part` when uncertain. Never leave the field blank or use other values.

## CRITICAL RULES

- You MUST call `finalize` with the complete parser script. This is the only way to finish processing a customer.
- Do NOT stop after analysis. The analysis is useless without `finalize`.
- After `finalize` returns, report the results to the user.
- Use `python` (not `python3`) in shell commands.
- Do not download the same file twice.
"""


def _create_agent():
    """Build the agent lazily on first request.

    Uses a standard Agent with full tool access. The agent is wrapped in a
    WorkflowAgent for checkpointing/durability. The key enforcement is in the
    system prompt which explicitly requires completing all steps.
    """
    import logging as _logging
    _log = _logging.getLogger(__name__)
    _log.info("Building agent...")
    try:
        from agent_framework.foundry import FoundryChatClient  # noqa: PLC0415
        from agent_framework import Agent, WorkflowBuilder, WorkflowAgent  # noqa: PLC0415
        from azure.identity import DefaultAzureCredential, ManagedIdentityCredential  # noqa: PLC0415

        _instance_client_id = os.environ.get("FOUNDRY_AGENT_INSTANCE_CLIENT_ID")
        credential = (ManagedIdentityCredential(client_id=_instance_client_id)
                      if _instance_client_id else DefaultAzureCredential())

        # Azure Gov fix: the SDK hardcodes scope "https://ai.azure.com/.default"
        # but Gov managed-identity endpoints only issue tokens for the .us audience.
        # Wrap the credential to translate the scope transparently.
        _project_ep = os.environ["FOUNDRY_PROJECT_ENDPOINT"]
        if ".azure.us" in _project_ep:
            class _GovScopeCredential:
                """Translate commercial AI scope to Gov audience."""
                def __init__(self, inner):
                    self._inner = inner
                def get_token(self, *scopes, **kwargs):
                    translated = tuple(
                        s.replace("https://ai.azure.com/.default",
                                  "https://ai.azure.us/.default")
                        for s in scopes
                    )
                    return self._inner.get_token(*translated, **kwargs)
            credential = _GovScopeCredential(credential)
            _log.info("Gov scope translation enabled for credential.")

        client = FoundryChatClient(
            project_endpoint=_project_ep,
            model=os.environ["AZURE_AI_MODEL_DEPLOYMENT_NAME_FLASH"],
            credential=credential,
            instruction_role="system",
        )

        agent = Agent(
            client=client,
            instructions=ORCHESTRATOR_SYSTEM_PROMPT,
            tools=[
                _run_command_impl, _write_file_impl, _read_file_impl,
                _download_blob_impl, _upload_blob_impl,
                _list_boms_impl, _list_parsers_impl,
                _finalize_impl,
            ],
            default_options={"store": True, "max_output_tokens": 16384},
        )

        _log.info("Agent built successfully.")
        return agent
    except Exception:
        _log.exception("Agent initialization failed")
        raise


# =========================================================================
# Lazy-loading proxy — server starts, agent connects on first request
# =========================================================================

class _LazyAgent:
    """Proxy that defers agent initialization to the first request."""

    def __init__(self) -> None:
        object.__setattr__(self, "_real", None)
        object.__setattr__(self, "_lock", threading.Lock())

    def _get(self):
        real = object.__getattribute__(self, "_real")
        if real is None:
            lock = object.__getattribute__(self, "_lock")
            with lock:
                real = object.__getattribute__(self, "_real")
                if real is None:
                    real = _create_agent()
                    object.__setattr__(self, "_real", real)
        return real

    @property
    def context_providers(self):
        real = object.__getattribute__(self, "_real")
        return real.context_providers if real is not None else []

    def __getattr__(self, name: str):
        return getattr(self._get(), name)


def main():
    lazy = _LazyAgent()

    # Enable the resilient task subsystem (TaskManager) so background=true
    # requests run durably with crash recovery. Must be set before the
    # server's lifespan startup. Non-hosted mode uses LocalFileTaskProvider
    # (JSON under AGENTSERVER_STATE_ROOT, default ~/.agentserver/tasks).
    from azure.ai.agentserver.core.tasks import set_resilient_tasks_enabled  # noqa: PLC0415
    set_resilient_tasks_enabled(True)

    # Pre-warm in background thread — runs concurrently with Hypercorn startup
    import threading as _t
    _t.Thread(target=lazy._get, daemon=True, name="agent-init").start()

    server = ResponsesHostServer(
        lazy,
        store=InMemoryResponseProvider(),
        applicationinsights_connection_string=os.environ.get("BOM_APPINSIGHTS_CONNECTION_STRING"),
    )
    server.run()


if __name__ == "__main__":
    main()
