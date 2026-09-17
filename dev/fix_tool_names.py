"""Fix tool function names to use @tool(name=...) decorator matching the prompt."""
import re

with open("main.py", encoding="utf-8") as f:
    content = f.read()

# 1. Remove the old tool functions and insert @tool-decorated versions
old_tools_block = """# Tool wrappers \u2014 these wrap internal helpers as callable functions
# that can be registered as Agent tools."""

new_tools_header = """# Tool wrappers \u2014 decorated with @tool(name=...) to match prompt names

from agent_framework._tools import tool as _tool_reg  # noqa: E402"""

content = content.replace(old_tools_block, new_tools_header)

# 2. Replace each old function definition with @tool-decorated version
replacements = [
    ("def _run_command_tool(command: str) -> str:", "@_tool_reg(name=\"run_command\")\ndef _run_command_impl(command: str) -> str:"),
    ("def _write_file_tool(path: str, content: str) -> str:", "@_tool_reg(name=\"write_file\")\ndef _write_file_impl(path: str, content: str) -> str:"),
    ("def _read_file_tool(path: str, start_line: int = 1, end_line: int | None = None) -> str:", "@_tool_reg(name=\"read_file\")\ndef _read_file_impl(path: str, start_line: int = 1, end_line: int | None = None) -> str:"),
    ("def _download_blob_tool(blob_path: str, container: str = \"boms\") -> str:", "@_tool_reg(name=\"download_blob\")\ndef _download_blob_impl(blob_path: str, container: str = \"boms\") -> str:"),
    ("def _upload_blob_tool(local_path: str, container: str = \"parsers\") -> str:", "@_tool_reg(name=\"upload_blob\")\ndef _upload_blob_impl(local_path: str, container: str = \"parsers\") -> str:"),
    ("def _list_boms_tool() -> str:", "@_tool_reg(name=\"list_boms\")\ndef _list_boms_impl() -> str:"),
    ("def _list_parsers_tool() -> str:", "@_tool_reg(name=\"list_parsers\")\ndef _list_parsers_impl() -> str:"),
]

for old, new in replacements:
    if old in content:
        content = content.replace(old, new)
    else:
        print(f"WARNING: Could not find: {old[:60]}")

# 3. Update the tools list in _create_agent where they are registered
agent_tools_old = """            tools=[
                _run_command_tool, _write_file_tool, _read_file_tool,
                _download_blob_tool, _upload_blob_tool,
                _list_boms_tool, _list_parsers_tool,
            ],"""

agent_tools_new = """            tools=[
                _run_command_impl, _write_file_impl, _read_file_impl,
                _download_blob_impl, _upload_blob_impl,
                _list_boms_impl, _list_parsers_impl,
            ],"""

if agent_tools_old in content:
    content = content.replace(agent_tools_old, agent_tools_new)
else:
    print("WARNING: Could not find tools list in _create_agent")

with open("main.py", "w", encoding="utf-8") as f:
    f.write(content)

print("Done. Tool names fixed.")