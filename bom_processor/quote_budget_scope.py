"""Parse server-owned budget enrollment scope; no I/O or dispatch.

Inputs: comma-separated UUID configuration. Output: immutable normalized set.
Verify with tests/test_quote_budget_scope.py; wildcards and malformed values fail.
"""
import re

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", re.I)


def parse_quote_scope(value: str) -> frozenset[str]:
    if not isinstance(value, str):
        raise ValueError("invalid author budget quote scope")
    if not value.strip():
        return frozenset()
    items = [item.strip().lower() for item in value.split(",")]
    if len(items) > 100 or any(not _UUID.fullmatch(item) for item in items):
        raise ValueError("invalid author budget quote scope")
    return frozenset(items)