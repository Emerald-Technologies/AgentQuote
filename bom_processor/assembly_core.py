"""Core-number rule: does cited text name a requested assembly the way a person would read it?

Our assembly numbers often wrap the customer's number with a customer-code prefix joined to
the first digits (ENE010-..., SNC006-...) and a letters-only build suffix (-TK); either, both
or neither may be present. The core is what remains: ENE010-0810-U2-TK -> 010 0810 U2. Text
names the assembly when it holds every core group, in order, with any separator, and is not
followed by another numeric dash group (010-0810-U2 never names 010-0810-U2-03).
One rule for the survey link and the approval binding (QuickQuote round 2, D1/D3, Nick 2026-10-10).
Pure; no I/O. Run python -m pytest tests/test_assembly_core.py.
"""
from __future__ import annotations

import re

_SPLIT = re.compile(r"[\s\-_./]+")
_GLUED_PREFIX = re.compile(r"^([A-Za-z]+)(\d[A-Za-z0-9]*)$")
_SEPARATOR = r"[\s\-_.]{0,2}"
MIN_CORE_GROUPS = 2
MIN_CORE_DIGITS = 5


def _groups(value: str) -> list[str]:
    return [part for part in _SPLIT.split(value.strip()) if part]


def core_groups(part_number: str) -> tuple[str, ...]:
    """The customer core of a requested part number; the full number when trimming leaves too little."""
    full = _groups(part_number)
    core = list(full)
    if core:
        glued = _GLUED_PREFIX.match(core[0])
        if glued:
            core[0] = glued.group(2)
    if len(core) > 1 and core[-1].isalpha():
        core.pop()
    # A short number is never over-trimmed: keep the full number instead.
    if len(core) < MIN_CORE_GROUPS or sum(ch.isdigit() for ch in "".join(core)) < MIN_CORE_DIGITS:
        return tuple(full)
    return tuple(core)


def _find(groups: tuple[str, ...] | list[str], text: str) -> str | None:
    if not groups:
        return None
    body = _SEPARATOR.join(re.escape(group) for group in groups)
    # Optional customer-code letters may be joined to a core that starts with a digit.
    prefix = r"(?:[A-Za-z]{1,8})?" if groups[0][:1].isdigit() else ""
    # Not followed by more letters/digits, nor by a numeric dash group (a sibling dash number).
    pattern = r"(?<![A-Za-z0-9])" + prefix + body + r"(?![A-Za-z0-9])(?![\-_]\d)"
    found = re.search(pattern, text, re.IGNORECASE)
    return found.group(0) if found else None


def find_assembly(part_number: str, text: str) -> str | None:
    """The text as written that names this part number (full or core), else None."""
    if not isinstance(part_number, str) or not isinstance(text, str) or not part_number.strip():
        return None
    return _find(_groups(part_number), text) or _find(core_groups(part_number), text)


def _digits(value: str) -> list[int]:
    return [int(part) for part in re.findall(r"\d+", value)]


def revision_contradicts(requested: str | None, found: str | None) -> bool:
    """True only for a clear difference: V10p1-B and v10p1 agree, v9 and v10 do not.

    Digit sequences are compared (one may extend the other: V10 and V10.1 do not contradict);
    two revisions without digits are compared as letters. Missing or mixed forms never contradict.
    """
    if not requested or not found or not requested.strip() or not found.strip():
        return False
    left, right = _digits(requested), _digits(found)
    if left and right:
        shorter, longer = sorted((left, right), key=len)
        return longer[:len(shorter)] != shorter
    if not left and not right:
        letters = lambda value: re.sub(r"[^a-z]", "", value.casefold().removeprefix("rev"))
        return letters(requested) != letters(found)
    return False
