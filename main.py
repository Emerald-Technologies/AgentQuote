# Copyright (c) Microsoft. All rights reserved.

import os
import pathlib
import subprocess

from agent_framework import Agent
from agent_framework._compaction import (
    CompactionProvider,
    ContextWindowCompactionStrategy,
)
from agent_framework.foundry import FoundryChatClient
from agent_framework_foundry_hosting import ResponsesHostServer
from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# All file tool operations are restricted to this directory (prevents path traversal).
# Override by setting WORKSPACE_ROOT in the environment.
WORKSPACE_ROOT = pathlib.Path(os.environ.get("WORKSPACE_ROOT", ".")).resolve()

# Maximum bytes of command output returned to the model to avoid token overflow.
_MAX_OUTPUT_BYTES = 4_000


def _safe_path(path: str) -> pathlib.Path:
    """Resolve path and raise if it escapes WORKSPACE_ROOT."""
    resolved = (WORKSPACE_ROOT / path).resolve()
    if not str(resolved).startswith(str(WORKSPACE_ROOT)):
        raise ValueError(f"Access denied: '{path}' is outside the workspace root.")
    return resolved


# ---------------------------------------------------------------------------
# Tools — registered on the Agent so the model can call them automatically.
# ---------------------------------------------------------------------------

def read_file(path: str, start_line: int = 1, end_line: int | None = None) -> str:
    """Read specific lines of a file. Use run_command with rg/grep to find line numbers first.
    Always specify start_line and end_line for large files to avoid token overflow.

    Args:
        path: File path relative to the workspace root.
        start_line: 1-based line number to start reading from.
        end_line: 1-based line number to stop reading at (inclusive).

    Returns:
        The file contents as a string, or an error message.
    """
    try:
        lines = _safe_path(path).read_text(encoding="utf-8").splitlines()
        end = end_line if end_line is not None else len(lines)
        return "\n".join(lines[start_line - 1 : end])
    except Exception as e:
        return f"Error reading '{path}': {e}"


def write_file(path: str, content: str) -> str:
    """Write content to a file, creating parent directories as needed.
    Overwrites the file if it already exists.

    Args:
        path: File path relative to the workspace root.
        content: The full content to write.

    Returns:
        Confirmation message or error.
    """
    try:
        target = _safe_path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"Written: {path}"
    except Exception as e:
        return f"Error writing '{path}': {e}"


def run_command(command: str) -> str:
    """Run a shell command and return its combined stdout and stderr output.
    Output is capped at 10 KB. Times out after 30 seconds.
    WARNING: runs with the same permissions as the agent process.

    Args:
        command: The shell command to execute.

    Returns:
        Combined stdout/stderr output, or an error/timeout message.
    """
    try:
        result = subprocess.run(
            command,
            shell=True,          # noqa: S602 — intentional; agent is sandboxed in cloud
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(WORKSPACE_ROOT),
        )
        output = (result.stdout + result.stderr).strip()
        if len(output.encode()) > _MAX_OUTPUT_BYTES:
            output = output.encode()[:_MAX_OUTPUT_BYTES].decode(errors="replace") + "\n... (output truncated)"
        return output or "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: command timed out after 30 seconds."
    except Exception as e:
        return f"Error running command: {e}"


def replace_in_file(path: str, old_str: str, new_str: str) -> str:
    """Replace an exact string occurrence in a file. Fails if the string is not found
    or appears more than once (to prevent unintended multi-site edits).
    Prefer this over write_file for targeted edits to existing files.

    Args:
        path: File path relative to the workspace root.
        old_str: The exact literal text to replace. Must appear exactly once in the file.
        new_str: The replacement text.

    Returns:
        Confirmation with the number of lines changed, or an error message.
    """
    try:
        target = _safe_path(path)
        content = target.read_text(encoding="utf-8")
        count = content.count(old_str)
        if count == 0:
            return f"Error: string not found in '{path}'. Check whitespace and indentation."
        if count > 1:
            return f"Error: string appears {count} times in '{path}'. Add more context to make it unique."
        new_content = content.replace(old_str, new_str, 1)
        target.write_text(new_content, encoding="utf-8")
        old_lines = old_str.count("\n") + 1
        new_lines = new_str.count("\n") + 1
        return f"Replaced in '{path}': {old_lines} line(s) → {new_lines} line(s)."
    except Exception as e:
        return f"Error replacing in '{path}': {e}"


SYSTEM_PROMPT = """You are a BOM Processing Agent for Emerald Technologies, an electronic contract manufacturer.

PRIMARY OBJECTIVE: Learn the formatting and structure of customer assembly BOMs and develop per-customer parser/normalizer scripts that output to a canonical quoting format.

## Canonical Target Schema (quoting module input)

The quoting module expects an Excel file matching `format/finalformat.xlsx` with these columns:
- line_number (int) — sequence number
- item_number (str) — customer's part/item identifier
- item_name (str) — description of the item
- revision (str) — revision letter/number
- item_category (str) — e.g. Part, Document, Assembly, PCB
- unit_of_measure (str) — EA, FT, IN, etc.
- quantity (float) — per-assembly quantity
- reference_designator (str) — PCB refdes (e.g. R1,R2,C1-C5)
- bom_notes (str) — production notes, RoHS status, etc.
- manufacturer_1 through manufacturer_8 (str) — manufacturer names
- manufacturer_item_number_1 through manufacturer_item_number_8 (str) — MPNs

## Workflow
1. When given a customer BOM file, first analyze its structure: identify headers, column meanings, data types, quirks (merged cells, multi-line rows, embedded notes). Use `run_command` with Python/pandas to inspect.
2. Map each customer column to the canonical schema above. Flag columns that don't map cleanly.
3. Write a parser script in `parsers/<customer_name>/parse.py` that:
   - Reads the customer's native BOM format (Excel, CSV, fixed-width, etc.)
   - Normalizes it to the canonical schema
   - Outputs an Excel file matching `format/finalformat.xlsx`
   - Logs warnings for unmappable data, missing required fields, and data quality issues
4. Validate: run the parser against the sample BOM and compare output to the canonical format.
5. Save sample BOMs in `boms/<customer_name>/` for reference.

## Script Design Rules
- Each customer gets a self-contained parser in `parsers/<customer_name>/parse.py`
- Parser must accept `--input <path>` and `--output <path>` CLI arguments
- Use `pandas` and `openpyxl` (already available)
- Handle common BOM quirks: header rows that aren't row 1, merged cells, empty rows, footer/summary rows, multi-sheet workbooks
- Add a docstring at the top explaining the customer's format and mapping decisions
- Never hard-code secrets or credentials

## Tone
- Be concise. Give the answer, not a story.
- When developing a parser, show the code and explain only non-obvious mapping decisions.
- Flag data quality issues immediately (missing quantities, unrecognized columns, ambiguous mappings)."""


# class RoutingFoundryChatClient(FoundryChatClient):
#     """Dynamically routes requests between Pro and Flash models based on difficulty."""
#     def __init__(self, pro_model: str, flash_model: str, **kwargs):
#         super().__init__(model=flash_model, **kwargs)
#         self.pro_model = pro_model
#         self.flash_model = flash_model
#
#     def get_response(self, messages, *args, **kwargs):
#         # Heuristic: analyze the last user message
#         last_user_text = ""
#         for m in reversed(messages):
#             role = getattr(m, "role", "") or (m.get("role") if isinstance(m, dict) else "")
#             if role == "user":
#                 last_user_text = getattr(m, "content", "") or (m.get("content") if isinstance(m, dict) else "")
#                 break
#
#         last_user_text = str(last_user_text).lower()
#         hard_markers = ["bug", "debug", "error", "refactor", "architect", "security", "complex", "why does", "explain why", "issue"]
#         is_hard = len(last_user_text) > 300 or any(m in last_user_text for m in hard_markers)
#
#         self.model = self.pro_model if is_hard else self.flash_model
#         return super().get_response(messages, *args, **kwargs)

def main():
    # Context-window compaction: when the conversation approaches the model's
    # 128K token limit, old tool-call groups are collapsed into summaries
    # (triggers at 30% budget) and the oldest messages are truncated
    # (triggers at 50% budget) to aggressively save tokens.
    compaction = ContextWindowCompactionStrategy(
        max_context_window_tokens=100_000,
        max_output_tokens=4_096,
        tool_eviction_threshold=0.3,
        truncation_threshold=0.6,
        keep_last_tool_call_groups=1,
    )

    client = FoundryChatClient(
        project_endpoint=os.environ["FOUNDRY_PROJECT_ENDPOINT"],
        model=os.environ["AZURE_AI_MODEL_DEPLOYMENT_NAME_FLASH"],
        credential=DefaultAzureCredential(),
        instruction_role="system",
        compaction_strategy=compaction,
    )

    agent = Agent(
        client=client,
        instructions=SYSTEM_PROMPT,
        tools=[
            read_file, write_file, replace_in_file, run_command,
        ],
        # CompactionProvider runs before every model call: if the accumulated
        # context nears the budget it compacts old tool results and truncates
        # stale messages so the model always works with a focused window.
        context_providers=[
            CompactionProvider(before_strategy=compaction),
        ],
        # History is managed by the hosting infrastructure
        # https://developers.openai.com/api/reference/resources/responses/methods/create
        default_options={"store": False},
    )

    server = ResponsesHostServer(agent)
    server.run()


if __name__ == "__main__":
    main()
