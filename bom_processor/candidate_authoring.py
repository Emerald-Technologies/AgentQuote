"""The deliberately narrow, non-executing candidate authoring boundary."""
from __future__ import annotations

import ast
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Protocol

from .core import BlobConflictError, CANONICAL_COLUMNS

MAX_CODE_BYTES = 1_000_000
MAX_SOURCE_BYTES = 50_000_000
# Version changes are identity changes: a replacement cannot reuse a
# contained candidate produced by an older authoring policy.
AUTHORING_VERSION = "candidate-authoring.v4"
CANONICAL_OUTPUT_RULES = (
    "write a real XLSX workbook to --output using pandas or openpyxl, never CSV or JSON",
    "write exactly the supplied canonical columns in the supplied order and at least one data row",
    "line_number must be consecutive integers starting at 1",
    "item_number, item_name, and item_category must be non-empty trimmed strings",
    "map source component taxonomy such as Capacitor, Resistor, or PCB assembly to canonical item_category='Part'; reserve item_category='Document' for document, drawing, or specification rows; do not require the source category to already be Part or Document",
    "item_category must be exactly Part or Document after applying the source taxonomy mapping",
    "quantity must be a finite number",
    "all populated strings must be trimmed; use None for unavailable optional values",
    "manufacturer_N and manufacturer_item_number_N must either both be populated or both be empty for N 1 through 8",
    "reopen the XLSX before exit and fail unless its ordered columns and row count are correct",
    "read only --input and write only --output; do not access network",
)
HTML_XLS_RULES = (
    "for source_subtype html_table_xls, use only stdlib html.parser.HTMLParser with bounded inert table extraction",
    "for source_subtype html_table_xls, never use pandas.read_html, network access, rendering, eval, or XML parsing",
    "for source_subtype html_table_xls, map extracted rows to the standard XLSX output format",
    "for source_subtype html_table_xls, preserve the actual source category in bom_notes when useful, but never fabricate a category or row",
    "map real source rows only; never fabricate fallback rows or substitute identity with UNKNOWN, N/A, TBD, TBC, or placeholders",
)
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_HASH = re.compile(r"^[0-9a-fA-F]{64}$")
_BANNED_NAMES = {"eval", "exec", "compile"}
_BANNED_MODULES = {
    "socket", "requests", "urllib", "http", "ftplib", "smtplib", "telnetlib",
    "subprocess", "os", "ctypes", "cffi", "importlib", "builtins", "multiprocessing",
}


class AuthoringError(ValueError):
    """Safe, stable failure returned by this boundary."""
    def __init__(self, code: str, rule_id: str | None = None):
        self.code = code
        self.rule_id = rule_id
        super().__init__(code)


class ModelAuthor(Protocol):
    def generate(self, prompt: "AuthoringPrompt", source_bytes: bytes,
                 prior_code: str | None = None,
                 failed_rule_id: str | None = None) -> str: ...


def _generate(model: ModelAuthor, prompt: AuthoringPrompt, source: bytes,
              prior_code: str | None = None,
              failed_rule_id: str | None = None) -> str:
    """Call old two-argument doubles unless correction context is required."""
    if prior_code is None:
        return model.generate(prompt, source)
    return model.generate(prompt, source, prior_code, failed_rule_id)


@dataclass(frozen=True)
class SourceAnalysis:
    """Only bounded metadata is placed in the prompt; source bytes are separate."""
    size_bytes: int
    sha256: str
    extension: str


@dataclass(frozen=True)
class AuthoringPrompt:
    canonical_columns: tuple[str, ...]
    rules: tuple[str, ...]
    structural_profile: dict[str, Any]
    source_analysis: SourceAnalysis
    prompt_version: str = AUTHORING_VERSION

    def __repr__(self) -> str:
        return (f"AuthoringPrompt(canonical_columns={self.canonical_columns!r}, rules={self.rules!r}, "
                f"structural_profile={self.structural_profile!r}, source_analysis={self.source_analysis!r}, "
                f"prompt_version={self.prompt_version!r})")


@dataclass(frozen=True)
class AuthoringResult:
    request_id: str
    candidate_id: str
    manifest_path: str
    status: str = "generated_unvalidated"


def _hash(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def _write(storage: Any, path: str, data: bytes, content_type: str) -> None:
    storage.upload_candidate_immutable(path, data, content_type)


def _policy(rule_id: str) -> AuthoringError:
    return AuthoringError("CANDIDATE_CODE_POLICY", rule_id)


def _validate_code(code: str, source_subtype: str = "") -> bytes:
    if not isinstance(code, str) or "\x00" in code:
        raise AuthoringError("CANDIDATE_CODE_INVALID")
    try:
        data = code.encode("utf-8")
        ast_tree = ast.parse(code, filename="parse.py")
    except (UnicodeEncodeError, SyntaxError):
        raise AuthoringError("CANDIDATE_CODE_INVALID") from None
    if len(data) > MAX_CODE_BYTES:
        raise _policy("code_size_limit")
    if "--input" not in code or "--output" not in code:
        raise _policy("missing_io_markers")
    for node in ast.walk(ast_tree):
        if isinstance(node, ast.ExceptHandler) and (node.type is None or
                (isinstance(node.type, ast.Name) and node.type.id in {"Exception", "BaseException"})):
            # Broad catches can hide parser failures.  Permit only an explicit
            # re-raise, never pass or a fabricated return value.
            if not node.body or any(not isinstance(statement, ast.Raise) for statement in node.body):
                raise _policy("broad_exception_handler")
        if isinstance(node, ast.Import):
            names = [alias.name.split(".")[0] for alias in node.names]
            if any(name in _BANNED_MODULES for name in names):
                raise _policy("banned_import")
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root in _BANNED_MODULES:
                raise _policy("banned_import")
        elif isinstance(node, ast.Call):
            name = node.func.id if isinstance(node.func, ast.Name) else (
                node.func.attr if isinstance(node.func, ast.Attribute) else "")
            if name in _BANNED_NAMES or (isinstance(node.func, ast.Attribute) and
                                         node.func.attr in {"system", "popen", "read_html"}):
                raise _policy("html_xls_banned_workbook_api")
            if source_subtype == "html_table_xls" and isinstance(node.func, ast.Attribute) and node.func.attr == "read_excel":
                raise _policy("html_xls_banned_workbook_api")
            if isinstance(node.func, ast.Name) and node.func.id == "open" and node.args:
                arg = node.args[0]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and (
                        arg.value.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:[\\/]", arg.value)):
                    raise _policy("absolute_path_access")
    return data


class CandidateAuthoringService:
    def __init__(self, request_id: str, storage: Any, model: ModelAuthor):
        if not isinstance(request_id, str) or not _ID.fullmatch(request_id):
            raise AuthoringError("INVALID_REQUEST_ID")
        self.request_id, self.storage, self.model = request_id, storage, model
    def author(self) -> AuthoringResult:
        path = f"candidates/requests/{self.request_id}.json"
        try:
            request = json.loads(self.storage.download_registry(path, 1_000_000).decode("utf-8"))
        except Exception:
            raise AuthoringError("REQUEST_UNAVAILABLE") from None
        self._validate_request(request)
        evidence = request["source_evidence"][0]
        try:
            source = self.storage.download_source(evidence["blob_path"], MAX_SOURCE_BYTES)
        except Exception:
            raise AuthoringError("SOURCE_UNAVAILABLE") from None
        if len(source) != evidence["size_bytes"] or hashlib.sha256(source).hexdigest() != evidence["sha256"].lower():
            raise AuthoringError("SOURCE_INTEGRITY")
        analysis = SourceAnalysis(len(source), hashlib.sha256(source).hexdigest(), evidence["blob_path"].rsplit(".", 1)[-1].lower())
        rules = CANONICAL_OUTPUT_RULES
        if request["profile"].get("source_subtype") == "html_table_xls":
            rules += HTML_XLS_RULES
        prompt = AuthoringPrompt(tuple(CANONICAL_COLUMNS), rules, request["profile"], analysis)
        source_subtype = request["profile"].get("source_subtype", "")
        try:
            generated = _generate(self.model, prompt, source)
            try:
                code = _validate_code(generated, source_subtype)
            except AuthoringError as failure:
                if failure.code != "CANDIDATE_CODE_POLICY":
                    raise
                # The rejected source is deliberately kept only in this stack
                # frame and is supplied to the model solely for one correction.
                try:
                    corrective = _generate(self.model, prompt, source, generated, failure.rule_id)
                except TypeError:
                    # Older injected doubles cannot perform correction; retain
                    # the original stable policy failure without another call.
                    raise failure
                code = _validate_code(corrective, source_subtype)
        except AuthoringError:
            raise
        except Exception:
            raise AuthoringError("MODEL_FAILED") from None
        code_hash = hashlib.sha256(code).hexdigest()
        signature = request["profile"].get("signature_hash", request["profile_hash"])
        candidate_id = _hash({"request_id": self.request_id, "code_sha256": code_hash,
                      "signature": signature, "authoring_version": AUTHORING_VERSION})
        base = f"candidates/{self.request_id}/{candidate_id}"
        manifest = {"version": AUTHORING_VERSION, "status": "generated_unvalidated", "request_id": self.request_id,
                    "candidate_id": candidate_id, "parser_key": request["parser_key"], "customer_key": request["customer_key"],
                    "profile_hash": request["profile_hash"], "source_sha256": evidence["sha256"], "source_size_bytes": evidence["size_bytes"],
                    "code_sha256": code_hash, "signature_suggestion": signature, "model_version": "injected", "prompt_version": AUTHORING_VERSION}
        manifest_bytes = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
        try:
            _write(self.storage, f"{base}/parse.py", code, "text/x-python")
            _write(self.storage, f"{base}/candidate-manifest.json", manifest_bytes, "application/json")
            event = json.dumps({"event": "candidate.generated", "request_id": self.request_id,
                                "candidate_id": candidate_id, "authoring_version": AUTHORING_VERSION},
                               sort_keys=True).encode()
            _write(self.storage, f"candidates/events/{self.request_id}/{candidate_id}.json", event, "application/json")
        except BlobConflictError:
            raise AuthoringError("CANDIDATE_CONFLICT") from None
        return AuthoringResult(self.request_id, candidate_id, f"{base}/candidate-manifest.json")

    @staticmethod
    def _validate_request(value: Any) -> None:
        required = {"version", "request_id", "parser_key", "customer_key", "profile_hash", "source_evidence", "profile", "status"}
        if not isinstance(value, dict) or not required <= value.keys() or value["status"] != "requested":
            raise AuthoringError("REQUEST_INVALID")
        if not _ID.fullmatch(value["request_id"]) or not _ID.fullmatch(value["parser_key"]) or not _ID.fullmatch(value["customer_key"]):
            raise AuthoringError("REQUEST_INVALID")
        if not _HASH.fullmatch(value["profile_hash"]) or not isinstance(value["profile"], dict):
            raise AuthoringError("REQUEST_INVALID")
        evidence = value["source_evidence"]
        if not isinstance(evidence, list) or len(evidence) != 1 or not isinstance(evidence[0], dict):
            raise AuthoringError("REQUEST_INVALID")
        item = evidence[0]
        if not isinstance(item.get("blob_path"), str) or type(item.get("size_bytes")) is not int or item["size_bytes"] < 0 or not _HASH.fullmatch(item.get("sha256", "")):
            raise AuthoringError("REQUEST_INVALID")