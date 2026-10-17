"""Known-unused package files (QuickQuote #134, #99 unit 3; Nick 2026-10-17, decision D9).

Clues, not rules. A file's extension, folder names and archive names mark it as known unused for
quoting, each with a reason: ODB++ data, PCB design source, 3D models, CAD drawings that duplicate
a PDF drawing next to them, and CAD viewer files. Nothing is deleted or hidden: callers list every
disregarded file with its reason, and a person can bring one back. Fail closed:
- spreadsheets and PDFs are never disregarded (a possible BOM or drawing);
- ODB++ with no Gerber or drill clue next to it may be the board's only fab data, so it is kept
  and a warning finding says so;
- a CAD drawing with no PDF drawing next to it may be the only drawing, so it is kept with a note.

Inputs: the reviewed rules file next to this module (package-file-relevance-rules.v1.json) and each
file's path as segments (the uploaded file name, then the path inside each archive). Output: for each
path None or {kind, reasonCode, reason}, plus findings. Pure, standard library only.

Mirrored byte for byte (module, rules and the shared cases file package-file-relevance-cases.v1.json):
QuickQuote QuickQuoteFunctions/quote_package/ and AgentQuote bom_processor/. The TypeScript copy is
QuickQuote "Run book/Quick Quote UI V1.0/lib/agentquote/package-file-relevance.ts". All three run the
shared cases. Run python -m pytest tests/test_package_file_relevance.py.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

RULES_FILE = "package-file-relevance-rules.v1.json"
RULES_VERSION = "package-file-relevance-rules.v1"
_REASON_CODE = re.compile(r"not_used_[a-z0-9_]{1,50}")
_KIND = re.compile(r"[a-z0-9_]{1,40}")


def _load() -> tuple[dict, str]:
    # Line endings are normalised so a Windows checkout and a Linux image pin the same hash.
    raw = (Path(__file__).with_name(RULES_FILE)).read_bytes().replace(bytes([13, 10]), bytes([10]))
    rules = json.loads(raw.decode("utf-8"))
    if not isinstance(rules, dict) or rules.get("version") != RULES_VERSION:
        raise ValueError("RELEVANCE_RULES_INVALID")
    kinds = [rules["odbpp"], rules["cadDrawing"], *rules["extensionKinds"], *rules["namedRules"]]
    for kind in kinds:
        if (not _KIND.fullmatch(str(kind.get("kind"))) or not _REASON_CODE.fullmatch(str(kind.get("reasonCode")))
                or not isinstance(kind.get("reason"), str) or not kind["reason"].strip()):
            raise ValueError("RELEVANCE_RULES_INVALID")
    for rule in rules["namedRules"]:
        if not isinstance(rule.get("id"), str) or not isinstance(rule.get("segmentPattern"), str):
            raise ValueError("RELEVANCE_RULES_INVALID")
    return rules, hashlib.sha256(raw).hexdigest()


RULES, RULES_SHA256 = _load()
_ODB = re.compile(RULES["odbpp"]["namePattern"])
_FAB_EXT = re.compile(RULES["fabClues"]["extensionPattern"])
_FAB_NAME = re.compile(RULES["fabClues"]["namePattern"])
_NAMED = [(rule, re.compile(rule["segmentPattern"])) for rule in RULES["namedRules"]]


def path_segments(chain: list[str] | tuple[str, ...]) -> list[str]:
    """Root file name, then each archive member path, split into its folders and name."""
    return [part for item in chain for part in str(item).split("/") if part]


def _ext(name: str) -> str:
    dot = name.rfind(".")
    return name[dot:].lower() if dot > 0 else ""


def _archive_stem(segment: str) -> tuple[str, bool]:
    low = segment.lower()
    for ext in RULES["archiveExtensions"]:
        if low.endswith(ext) and len(low) > len(ext):
            low = low[:-len(ext)]
            return (low[:-4] if low.endswith(".tar") and len(low) > 4 else low), True
    return low, False


def _stem(name: str) -> str:
    dot = name.rfind(".")
    return (name[:dot] if dot > 0 else name).lower()


def _odb_marker(low: list[str]) -> int | None:
    """Index of the first folder or archive whose name says ODB++, else None."""
    for index, segment in enumerate(low):
        stem, archive = _archive_stem(segment)
        if index == len(low) - 1 and not archive:
            return None
        if _ODB.search(stem):
            return index
    return None


def _job_roots(lows: list[list[str]]) -> set[tuple[str, ...]]:
    """ODB++ job folders recognised by their structure: <job>/matrix/matrix or <job>/steps/<step>/layers/..."""
    roots: set[tuple[str, ...]] = set()
    for low in lows:
        if len(low) >= 3 and low[-1] == "matrix" and low[-2] == "matrix":
            roots.add(tuple(low[:-2]))
        for k in range(1, len(low) - 3):
            if low[k] == "steps" and low[k + 2] == "layers":
                roots.add(tuple(low[:k]))
    return roots


def _starts(low: list[str], prefix: tuple[str, ...] | list[str]) -> bool:
    return len(low) >= len(prefix) and tuple(low[:len(prefix)]) == tuple(prefix)


def _fab_clue(low: list[str], scope: tuple[str, ...]) -> bool:
    ext = _ext(low[-1])
    if ext in RULES["neverDisregardExtensions"] or _disregard_extension(ext):
        return False
    if ext in RULES["fabClues"]["extensions"] or _FAB_EXT.fullmatch(ext):
        return True
    return any(_FAB_NAME.search(segment) for segment in low[len(scope):])


def _disregard_extension(ext: str) -> bool:
    return any(ext in kind["extensions"] for kind in RULES["extensionKinds"]) or ext in RULES["cadDrawing"]["extensions"]


def _label(kind: dict) -> dict:
    return {"kind": kind["kind"], "reasonCode": kind["reasonCode"], "reason": kind["reason"]}


def classify(paths: list[list[str]]) -> tuple[list[dict | None], list[dict]]:
    """paths: each file's segments. Returns (one result per path, findings sorted by code and scope)."""
    lows = [[segment.lower() for segment in path] for path in paths]
    if any(not low for low in lows):
        raise ValueError("RELEVANCE_PATH_INVALID")
    roots = _job_roots(lows)
    markers: list[int | None] = []
    for low in lows:
        marker = _odb_marker(low)
        for root in roots:
            if _starts(low, root) and (marker is None or len(root) - 1 < marker):
                marker = len(root) - 1
        markers.append(marker)
    result: list[dict | None] = [None] * len(paths)
    odb: dict[tuple[str, ...], list[int]] = {}
    cad: list[int] = []
    for index, low in enumerate(lows):
        ext = _ext(low[-1])
        if ext in RULES["neverDisregardExtensions"]:
            continue
        named = next((rule for rule, pattern in _NAMED if any(pattern.search(segment) for segment in low)), None)
        if named is not None:
            result[index] = _label(named)
        elif markers[index] is not None:
            odb.setdefault(tuple(low[:markers[index]]), []).append(index)
        elif ext in RULES["cadDrawing"]["extensions"]:
            cad.append(index)
        else:
            kind = next((kind for kind in RULES["extensionKinds"] if ext in kind["extensions"]), None)
            if kind is not None:
                result[index] = _label(kind)
    findings: list[dict] = []
    for scope, members in odb.items():
        clue = any(markers[other] is None and _starts(lows[other], scope) and _fab_clue(lows[other], scope)
                   for other in range(len(lows)))
        if clue:
            for index in members:
                result[index] = _label(RULES["odbpp"])
            continue
        first = min(members)
        findings.append({"code": "ONLY_FAB_DATA_KEPT", "severity": "warning", "kind": "odbpp",
                         "scope": "/".join(paths[first][:len(scope)]), "count": len(members),
                         "message": "ODB++ data was kept: no Gerber or drill files were found next to it, "
                                    "so it may be this board's only fab data."})
    pdfs = [index for index, low in enumerate(lows) if _ext(low[-1]) == ".pdf"]
    kept: dict[tuple[str, ...], list[int]] = {}
    for index in cad:
        low = lows[index]
        folder = low[:-1]
        scope = folder[:-1] if len(folder) >= 2 else folder
        stem = _stem(low[-1])
        if any(_starts(lows[other], scope) or _stem(lows[other][-1]) == stem for other in pdfs):
            result[index] = _label(RULES["cadDrawing"])
        else:
            kept.setdefault(tuple(scope), []).append(index)
    for scope, members in kept.items():
        first = min(members)
        findings.append({"code": "CAD_DRAWING_KEPT", "severity": "information", "kind": "cad_drawing",
                         "scope": "/".join(paths[first][:len(scope)]), "count": len(members),
                         "message": "CAD drawings were kept: no PDF drawing was found next to them, "
                                    "so they may be the only drawings."})
    findings.sort(key=lambda item: (item["code"], item["scope"]))
    return result, findings


def is_not_used(category: str) -> bool:
    """Whether a category is the code-owned reason code given to a disregarded file."""
    return isinstance(category, str) and _REASON_CODE.fullmatch(category) is not None


def reason_for(reason_code: str) -> str | None:
    """The reviewed reason for a reason code, or None when the rules file does not know it."""
    kinds = [RULES["odbpp"], RULES["cadDrawing"], *RULES["extensionKinds"], *RULES["namedRules"]]
    return next((kind["reason"] for kind in kinds if kind["reasonCode"] == reason_code), None)
