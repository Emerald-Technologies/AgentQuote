# Copyright (c) Microsoft. All rights reserved.

import fnmatch
import ipaddress
import os
import pathlib
import shutil
import subprocess
import urllib.parse
import urllib.request

from agent_framework import Agent
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
_MAX_OUTPUT_BYTES = 10_000


def _safe_path(path: str) -> pathlib.Path:
    """Resolve path and raise if it escapes WORKSPACE_ROOT."""
    resolved = (WORKSPACE_ROOT / path).resolve()
    if not str(resolved).startswith(str(WORKSPACE_ROOT)):
        raise ValueError(f"Access denied: '{path}' is outside the workspace root.")
    return resolved


# ---------------------------------------------------------------------------
# Tools — registered on the Agent so the model can call them automatically.
# ---------------------------------------------------------------------------

def read_file(path: str) -> str:
    """Read the full text contents of a file.

    Args:
        path: File path relative to the workspace root.

    Returns:
        The file contents as a string, or an error message.
    """
    try:
        return _safe_path(path).read_text(encoding="utf-8")
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


def list_directory(path: str = ".") -> str:
    """List files and subdirectories at a path relative to the workspace root.

    Args:
        path: Directory path relative to the workspace root. Defaults to workspace root.

    Returns:
        Newline-separated list of entries (directories prefixed with [DIR]), or an error.
    """
    try:
        target = _safe_path(path)
        if not target.is_dir():
            return f"Not a directory: {path}"
        entries = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name))
        lines = [f"{'[DIR]' if e.is_dir() else '     '} {e.name}" for e in entries]
        return "\n".join(lines) if lines else "(empty)"
    except Exception as e:
        return f"Error listing '{path}': {e}"


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


def get_directory_tree(path: str = ".", depth: int = 3) -> str:
    """Return a recursive tree view of a directory, up to a given depth.

    Args:
        path: Directory path relative to the workspace root. Defaults to workspace root.
        depth: Maximum depth to recurse. Defaults to 3.

    Returns:
        Indented tree string, or an error message.
    """
    try:
        root = _safe_path(path)
        if not root.is_dir():
            return f"Not a directory: {path}"

        lines: list[str] = [root.name + "/"]

        def _recurse(directory: pathlib.Path, prefix: str, current_depth: int) -> None:
            if current_depth > depth:
                return
            entries = sorted(directory.iterdir(), key=lambda p: (not p.is_dir(), p.name))
            for i, entry in enumerate(entries):
                connector = "└── " if i == len(entries) - 1 else "├── "
                lines.append(prefix + connector + entry.name + ("/" if entry.is_dir() else ""))
                if entry.is_dir():
                    extension = "    " if i == len(entries) - 1 else "│   "
                    _recurse(entry, prefix + extension, current_depth + 1)

        _recurse(root, "", 1)
        return "\n".join(lines)
    except Exception as e:
        return f"Error building tree for '{path}': {e}"


def search_files(pattern: str, glob: str = "**/*") -> str:
    """Search file contents for lines matching a text pattern, within files matched by a glob.
    Case-insensitive. Returns up to 200 matches to avoid token overflow.

    Args:
        pattern: Text to search for (case-insensitive substring match).
        glob: Glob pattern to filter which files are searched. Defaults to all files.
               Example: "**/*.py" to search only Python files.

    Returns:
        Matches formatted as "path:line_number: line_content", or a message if nothing found.
    """
    try:
        matches: list[str] = []
        needle = pattern.lower()
        for file in sorted(WORKSPACE_ROOT.glob(glob)):
            if not file.is_file():
                continue
            try:
                for i, line in enumerate(file.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                    if needle in line.lower():
                        rel = file.relative_to(WORKSPACE_ROOT)
                        matches.append(f"{rel}:{i}: {line.rstrip()}")
                        if len(matches) >= 200:
                            matches.append("... (results truncated at 200 matches)")
                            return "\n".join(matches)
            except Exception:
                continue
        return "\n".join(matches) if matches else f"No matches for '{pattern}'."
    except Exception as e:
        return f"Error searching files: {e}"


def find_symbol(name: str) -> str:
    """Search all source files for definitions or usages of a symbol (function, class, variable).
    Looks for common definition patterns across Python, JS/TS, and similar languages.

    Args:
        name: The symbol name to find (exact, case-sensitive).

    Returns:
        Matching lines with file path and line number, or a not-found message.
    """
    # Patterns that typically indicate a definition
    definition_patterns = [
        f"def {name}",       # Python function
        f"class {name}",     # Python/JS class
        f"function {name}",  # JS/TS function
        f"const {name} =",   # JS/TS const
        f"let {name} =",     # JS/TS let
        f"var {name} =",     # JS/TS var
        f"{name} =",         # general assignment
    ]
    source_globs = ["**/*.py", "**/*.js", "**/*.ts", "**/*.jsx", "**/*.tsx",
                    "**/*.cs", "**/*.java", "**/*.go", "**/*.rb", "**/*.rs"]
    try:
        matches: list[str] = []
        seen: set[str] = set()
        for glob in source_globs:
            for file in sorted(WORKSPACE_ROOT.glob(glob)):
                if not file.is_file():
                    continue
                try:
                    for i, line in enumerate(file.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                        key = f"{file}:{i}"
                        if key in seen:
                            continue
                        stripped = line.strip()
                        if name in stripped and any(stripped.startswith(p) or f" {name}" in stripped or f"({name}" in stripped for p in definition_patterns):
                            seen.add(key)
                            rel = file.relative_to(WORKSPACE_ROOT)
                            matches.append(f"{rel}:{i}: {line.rstrip()}")
                except Exception:
                    continue
        # Fall back to plain search if no definition hits
        if not matches:
            return search_files(name, "**/*.py")
        return "\n".join(matches)
    except Exception as e:
        return f"Error finding symbol '{name}': {e}"


def _run_git(args: list[str]) -> str:
    """Run a git subcommand in WORKSPACE_ROOT. Returns stdout or an error."""
    try:
        result = subprocess.run(
            ["git"] + args,
            capture_output=True,
            text=True,
            timeout=15,
            cwd=str(WORKSPACE_ROOT),
        )
        output = (result.stdout + result.stderr).strip()
        return output or "(no output)"
    except FileNotFoundError:
        return "Error: git is not installed or not on PATH."
    except subprocess.TimeoutExpired:
        return "Error: git command timed out."
    except Exception as e:
        return f"Error running git: {e}"


def git_status() -> str:
    """Return the current git status of the workspace (staged, unstaged, untracked files).

    Returns:
        Output of `git status --short`, or an error message.
    """
    return _run_git(["status", "--short"])


def git_diff(path: str = "") -> str:
    """Return the unstaged diff for the workspace or a specific file.
    Output is capped at 10 KB.

    Args:
        path: Optional file path relative to the workspace root.
              If omitted, returns the full workspace diff.

    Returns:
        Unified diff output, or an error message.
    """
    args = ["diff"]
    if path:
        try:
            safe = _safe_path(path)
            args.append(str(safe))
        except ValueError as e:
            return str(e)
    output = _run_git(args)
    if len(output.encode()) > _MAX_OUTPUT_BYTES:
        output = output.encode()[:_MAX_OUTPUT_BYTES].decode(errors="replace") + "\n... (diff truncated)"
    return output


def get_git_log(n: int = 10) -> str:
    """Return the last N git commits with hash, author, date, and message.

    Args:
        n: Number of commits to return. Defaults to 10, capped at 50.

    Returns:
        Formatted commit log, or an error message.
    """
    n = min(max(1, n), 50)  # clamp to 1-50
    return _run_git(["log", f"-{n}", "--oneline", "--decorate"])


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


def delete_file(path: str) -> str:
    """Delete a file or empty directory at the given path.

    Args:
        path: File or empty directory path relative to the workspace root.

    Returns:
        Confirmation message or error.
    """
    try:
        target = _safe_path(path)
        if target.is_dir():
            target.rmdir()  # only removes empty dirs — prevents accidental tree deletion
            return f"Deleted directory: {path}"
        target.unlink()
        return f"Deleted: {path}"
    except Exception as e:
        return f"Error deleting '{path}': {e}"


def move_file(src: str, dst: str) -> str:
    """Move or rename a file or directory within the workspace.

    Args:
        src: Source path relative to the workspace root.
        dst: Destination path relative to the workspace root.
             Parent directories are created automatically.

    Returns:
        Confirmation message or error.
    """
    try:
        src_path = _safe_path(src)
        dst_path = _safe_path(dst)
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src_path), str(dst_path))
        return f"Moved: {src} → {dst}"
    except Exception as e:
        return f"Error moving '{src}' to '{dst}': {e}"


# Private URL allowlist check — blocks private IPs to prevent SSRF (OWASP A10)
_BLOCKED_SCHEMES = {"file", "ftp", "gopher"}


def _check_url_safety(url: str) -> str | None:
    """Return an error string if the URL is disallowed, else None."""
    try:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return f"Blocked scheme '{parsed.scheme}': only http and https are allowed."
        host = parsed.hostname or ""
        # Block localhost and loopback
        if host in ("localhost", "127.0.0.1", "::1") or host.endswith(".local"):
            return f"Blocked: requests to '{host}' are not allowed."
        # Block private/link-local IP ranges
        try:
            addr = ipaddress.ip_address(host)
            if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved:
                return f"Blocked: requests to private IP '{host}' are not allowed."
        except ValueError:
            pass  # host is a domain name, not an IP — allow it
        return None
    except Exception as e:
        return f"Invalid URL: {e}"


def http_request(url: str, method: str = "GET", headers: dict | None = None, body: str = "") -> str:
    """Make an HTTP request and return the response body.
    Only http/https to public hosts are allowed — private IPs and localhost are blocked.
    Response body is capped at 10 KB.

    Args:
        url: The full URL to request.
        method: HTTP method (GET, POST, PUT, PATCH, DELETE). Defaults to GET.
        headers: Optional dict of request headers.
        body: Optional request body string (for POST/PUT/PATCH).

    Returns:
        HTTP status line + response body, or an error message.
    """
    error = _check_url_safety(url)
    if error:
        return error
    try:
        data = body.encode("utf-8") if body else None
        req = urllib.request.Request(
            url,
            data=data,
            headers=headers or {},
            method=method.upper(),
        )
        with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310 — URL safety checked above
            raw = resp.read(_MAX_OUTPUT_BYTES)
            text = raw.decode("utf-8", errors="replace")
            truncated = " (truncated)" if len(raw) == _MAX_OUTPUT_BYTES else ""
            return f"HTTP {resp.status} {resp.reason}{truncated}\n\n{text}"
    except Exception as e:
        return f"Error making request to '{url}': {e}"


SYSTEM_PROMPT = """You are an expert coding assistant powered by DeepSeek. Your role is to help developers write, review, debug, and understand code across all programming languages and frameworks.

You can:
- Write clean, idiomatic code in any language when asked
- Review and improve existing code (performance, readability, security, best practices)
- Debug errors and explain the root cause
- Explain concepts, patterns, and algorithms clearly
- Generate unit tests and documentation
- Help with architecture and design decisions
- Suggest libraries, tools, and frameworks for a given task
- Read, write, and make targeted edits to files in the workspace
- Delete and move/rename files
- Search file contents across the workspace, explore directory trees, and find symbol definitions
- Run shell commands
- Make HTTP requests to external APIs and URLs
- View git status, diffs, and commit history

When writing code:
- Always prefer clear, readable code over clever one-liners
- Follow language-specific conventions and best practices (PEP8 for Python, etc.)
- Include brief inline comments for non-obvious logic
- Highlight potential security concerns (OWASP Top 10 awareness)
- Keep answers focused and concise unless depth is requested

When working with files:
- Use get_directory_tree first to understand a repo's structure before diving in
- Use search_files to find relevant code before reading entire files
- Use find_symbol to locate function/class definitions and usages
- Use read_file to understand existing code before suggesting changes
- Use replace_in_file for targeted edits to existing files — always include enough context to be unique
- Use write_file only to create new files or fully replace a file's content
- Use delete_file and move_file for file management
- Use http_request to fetch documentation, call APIs, or retrieve external data
- Use git_status and git_diff to understand what has changed before reviewing
- Use run_command sparingly — prefer the dedicated tools when possible

When debugging:
- Identify the root cause, not just the symptom
- Explain why the issue occurred
- Provide a corrected version with explanation

Always ask clarifying questions if the request is ambiguous before writing code.
Reference the AGENTS.md documentation if present for any questions about your capabilities or limitations."""


def main():
    client = FoundryChatClient(
        project_endpoint=os.environ["FOUNDRY_PROJECT_ENDPOINT"],
        model=os.environ["AZURE_AI_MODEL_DEPLOYMENT_NAME"],
        credential=DefaultAzureCredential(),
    )

    agent = Agent(
        client=client,
        instructions=SYSTEM_PROMPT,
        tools=[
            read_file, write_file, replace_in_file, list_directory, run_command,
            get_directory_tree, search_files, find_symbol,
            git_status, git_diff, get_git_log,
            delete_file, move_file, http_request,
        ],
        # History is managed by the hosting infrastructure
        # https://developers.openai.com/api/reference/resources/responses/methods/create
        default_options={"store": False},
    )

    server = ResponsesHostServer(agent)
    server.run()


if __name__ == "__main__":
    main()
