from types import SimpleNamespace

from bom_processor.core import CANONICAL_COLUMNS
from bom_processor.foundry_author import (SYSTEM_INSTRUCTION, FoundryModelAuthor,
                                          build_preview, strip_code_fences)


def test_preview_is_bounded_and_redacts_sensitive_text():
    source = (b"name,email,url,token\npart,a@example.com,https://example.test/a,"
              b"abcdefghijklmnopqrstuvwxyz1234567890\n")
    preview = build_preview(source, ".csv")
    assert len(preview) <= 8000
    assert "a@example.com" not in preview
    assert "https://example.test" not in preview
    assert "abcdefghijklmnopqrstuvwxyz1234567890" not in preview
    assert "name" in preview


def test_binary_input_is_not_raw_prompt_content():
    preview = build_preview(bytes(range(256)) * 100, ".xlsx")
    assert len(preview) <= 8000
    assert "<unreadable workbook>" in preview


def test_html_xls_preview_extracts_bounded_table_rows():
    source = (b'<html><body><table><tr><th>Category</th><th>Part number</th><th>Name</th></tr>'
              b'<tr><td>Capacitor</td><td>C-100</td><td>10uF ceramic</td></tr>'
              b'<tr><td>PCB assembly</td><td>ASM-1</td><td>Control board</td></tr>'
              b'</table></body></html>')
    preview = build_preview(source, ".xls")
    assert "Category\tPart number\tName" in preview
    assert "Capacitor\tC-100\t10uF ceramic" in preview
    assert "PCB assembly\tASM-1\tControl board" in preview
    assert "<unreadable workbook>" not in preview


def test_factory_injection_has_no_tools_and_extracts_response_text():
    seen = {}

    class Agent:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        async def run(self, text):
            seen["prompt"] = text
            return SimpleNamespace(output_text="```python\nprint('ok')\n```")

    prompt = SimpleNamespace(canonical_columns=("item",), rules=("write XLSX",),
                             source_analysis=SimpleNamespace(extension="csv"))
    result = FoundryModelAuthor(client=object(), agent_factory=Agent).generate(prompt, b"item\nX\n")
    assert result == "print('ok')"
    assert seen["tools"] == []
    assert "item" in seen["prompt"]
    assert "write XLSX" in seen["prompt"]


def test_author_contract_states_validator_critical_rules():
    seen = {}

    class Agent:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        async def run(self, text):
            seen["prompt"] = text
            return SimpleNamespace(output_text="print('ok')")

    rules = (
        "line_number consecutive integers starting at 1",
        "item_category exactly Part or Document",
        "manufacturer pairs must match",
    )
    prompt = SimpleNamespace(canonical_columns=tuple(CANONICAL_COLUMNS), rules=rules,
                             source_analysis=SimpleNamespace(extension="csv"))
    FoundryModelAuthor(client=object(), agent_factory=Agent).generate(prompt, b"item\nX\n")
    positions = [seen["prompt"].index(column) for column in CANONICAL_COLUMNS]
    assert positions == sorted(positions)
    assert all(rule in seen["prompt"] for rule in rules)
    assert "real XLSX" in SYSTEM_INSTRUCTION
    assert "Reopen" in SYSTEM_INSTRUCTION
    assert "Capacitor" in SYSTEM_INSTRUCTION
    assert "reserve item_category='Document'" in SYSTEM_INSTRUCTION


def test_strip_fences_only_removes_outer_fence():
    assert strip_code_fences("```python\ncode\n```") == "code"
    assert strip_code_fences(" prose\ncode") == " prose\ncode"


def test_corrective_prompt_contains_prior_code_and_html_parser_constraints():
    seen = {}

    class Agent:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        async def run(self, text):
            seen["prompt"] = text
            return SimpleNamespace(output_text="print('fixed')")

    prompt = SimpleNamespace(canonical_columns=("item",), rules=("rule",),
                             source_analysis=SimpleNamespace(extension="xls"))
    FoundryModelAuthor(client=object(), agent_factory=Agent).generate(
        prompt, b"item\nX\n", "import pandas as pd\npd.read_html(x)",
        "html_xls_banned_workbook_api")
    assert "Previous candidate" in seen["prompt"]
    assert "[html_xls_banned_workbook_api]" in seen["prompt"]
    assert "HTMLParser" in seen["prompt"]
    assert "read_html" in seen["prompt"]