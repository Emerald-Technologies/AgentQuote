"""The deliberately narrow, non-executing candidate authoring boundary."""
from __future__ import annotations

import ast
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Protocol

from .core import BlobConflictError, CANONICAL_COLUMNS
from .candidates import unique_fixtures
from .author_failure import AuthorFailure, diagnostic_fields, failure_stage

MAX_CODE_BYTES = 1_000_000
MAX_SOURCE_BYTES = 50_000_000
# Version changes are identity changes: a replacement cannot reuse a
# contained candidate produced by an older authoring policy.
# v9/v10 (plan 8.12): output rules carry manufacturer/MPN pairs beyond 8 and fail above 50.
# v9/v10 also (plan 8.8 step 4a, Nick 2026-10-09): the parser writes a --row-map that
# validation checks against every source row.
AUTHORING_VERSION = "candidate-authoring.v9"
ASSOCIATION_AUTHORING_VERSION = "candidate-authoring.v10"
ROW_MAP_AUTHORING_VERSIONS = frozenset({AUTHORING_VERSION, ASSOCIATION_AUTHORING_VERSION})
ROW_MAP_RULE = (
    "also accept an optional --row-map PATH argument (never required: approved parsers run without it); "
    "whenever it is given, write it as UTF-8 JSON: {\"version\": \"bom-row-map.v2\", \"lines\": [...], "
    "\"skipped\": [...], \"ignored_columns\": [...]}. Sheet names, rows and columns refer to the --input "
    "workbook (1-based, the row numbers a spreadsheet shows; for HTML tables the n-th table row). "
    "lines has one entry per output data row: {\"output_row\": the output worksheet row number (first data "
    "row is 2), \"sheet\", \"rows\": a flat list of the integer row numbers on that sheet that make this "
    "line, including alternate-manufacturer rows (for example [6] or [6, 7]; the sheet name goes only in "
    "\"sheet\", never [sheet, row] pairs), \"fields\": {output column name: [source row, source column] of the cell the value came from}}. "
    "When the BOM shows sub-assemblies (a level column, item numbers such as 1, 1.1, 1.1.1, indented text, or a "
    "column naming each row's parent part number), give every line \"parent\": {\"line\": the output_row of its "
    "parent line (the number that parent line has as its own output_row, so 2 or more; never its line_number, "
    "its position in lines or a source row), or null for a line directly under the quoted assembly, \"from\": "
    "[source row, source column] of the cell that shows it, \"by\": one of level, number, parent, indent}, using "
    "one kind and one column for every line; validation works out each parent from those cells. "
    "When one BOM lists several assemblies, each with its own lines (for example two boards in one sheet with a "
    "parent part number column), each assembly's own row has \"line\": null and every line under it points at "
    "that row; a parent cell naming the row's own part number marks such a top-level row. Worked example (A048): "
    "heading in row 1, board A in source row 2 (output_row 2) with its lines in rows 3-7 (output_rows 3-7), "
    "board B in source row 8 (output_row 8) with its lines in rows 9-13: output_rows 2 and 8 have \"line\": null, "
    "output_rows 3-7 have \"line\": 2 and output_rows 9-13 have \"line\": 8. "
    "Link every populated output column except line_number and item_category; use [row, column, \"part\"] "
    "when the value is one separated part of a combined cell; use [row, column, \"code\"] for a manufacturer "
    "name written without the customer's short code that stands in front of it in the cell (the cell "
    "'63 ACME INC.' written as 'ACME INC.'); use {\"lookup\": [sheet, row, column], \"key\": column} for a value "
    "kept on another sheet in a row whose key column holds the line's part number (for example manufacturer part "
    "numbers on a separate manufacturer sheet), carrying every such row for that part; use {\"derived\": reason} only for "
    "unit_of_measure or bom_notes values not copied from one cell. Every listed row must supply at least "
    "one linked value, and both halves of a manufacturer/MPN pair come from the same source row. A linked "
    "cell must hold exactly the written value (ignoring outer spaces): a value with added words, such as "
    "'Source category: Capacitor' in bom_notes, is not copied from one cell and is cited as {\"derived\": "
    "reason}, and a source column then cited by no field goes in ignored_columns; cite each manufacturer and MPN at its own column, and use \"part\" only when that cell holds "
    "several values separated by delimiters. skipped "
    "lists every other non-empty source row: {\"sheet\", \"rows\": [[first, last], ...], \"reason\": one of header, "
    "title, blank, total, note, section, signature, other_sheet, other, \"rationale\"}. ignored_columns lists "
    "every source column holding data in BOM lines that no linked output field cites, including a column the "
    "parser only reads to decide item_category or which rows to keep: {\"sheet\", \"column\", \"reason\"}. "
    "Build the row map from the same reading that writes the output; validation checks it against every "
    "row of the source and reports each unaccounted, double-counted or mis-cited row by location, so do not "
    "check row accounting yourself or stop over it"
)
EXCEPTION_POLICY_RULE = (
    "never use bare except, except Exception or except BaseException; catch specific exceptions such as ValueError or KeyError, or let errors propagate; a broad handler may contain only a raise statement, never pass, print, return, a fallback value or sys.exit"
)
# Enforced by _validate_code (rule html_xls_banned_workbook_api) for every source type; the A035
# author broke it unseen (round 2, item 4). read_excel is banned only for html_table_xls sources.
BANNED_CALL_RULE = (
    "never call the built-in eval, exec or compile, os.system, popen or pandas.read_html, for any source type "
    "(module functions with those names, such as re.compile, are fine)"
)
# Round 12 (A044): a rewrite refused by the code policy produced no parser, and the retry is told why.
POLICY_RETRY_RULE = (
    "IMPORTANT: the previous rewrite of this parser was refused by the code policy before it could run; "
    "follow every import and call rule exactly: import only standard parsing modules, pandas, openpyxl and, "
    "for a legacy binary .xls, xlrd "
    "(never socket, requests, urllib, http, ftplib, smtplib, telnetlib, subprocess, os, ctypes, cffi, importlib, "
    "builtins or multiprocessing), never call the built-in eval, exec or compile, and never use a broad except"
)
HTML_XLS_WORKBOOK_RULE = (
    "for source_subtype html_table_xls, never call pandas.read_excel or open the file as a workbook; "
    "it is an HTML table, so parse it with html.parser.HTMLParser"
)
# A046 (Nick 2026-10-14): authors opened a legacy .xls with openpyxl and learned only from the crash.
OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
LEGACY_XLS_RULE = (
    "the input is a legacy binary Excel workbook (OLE2/BIFF .xls), which openpyxl cannot open: read it with "
    "xlrd.open_workbook(file_contents=...) (or pandas.read_excel with engine='xlrd'); read cell values with "
    "sheet.row_values(row), sheet.cell_value(row, column) or cell.value, never an xlrd Cell object itself (its "
    "text is not the cell's value, so headings never match; A047); xlrd counts rows and "
    "columns from 0, while the row map uses the 1-based numbers a spreadsheet shows, so add 1 to each"
)
CANONICAL_OUTPUT_RULES = (
    "write a real XLSX workbook to --output using pandas or openpyxl, never CSV or JSON",
    "write exactly the supplied canonical columns in the supplied order and at least one data row; when any line has more than 8 manufacturer/MPN pairs, append manufacturer_9, manufacturer_item_number_9, and so on in that order up to the widest line (at most 50 pairs), the same columns for every row, never dropping a pair",
    "line_number must be consecutive integers starting at 1",
    "item_number, item_name, and item_category must be non-empty trimmed strings",
    "map source component taxonomy such as Capacitor, Resistor, or PCB assembly to canonical item_category='Part'; reserve item_category='Document' for document, drawing, or specification rows; do not require the source category to already be Part or Document",
    "item_category must be exactly Part or Document after applying the source taxonomy mapping",
    "Part quantity must be a finite number; a quantity of 0 is valid and kept as 0 (for example do-not-populate or the assembly's own top line), never dropped or changed; Document quantity is not priced, preserve supplied numeric values or None when absent, never invent a quantity",
    "for Document rows, use explicit source labels and row context to put the actual document ID in item_number and its revision in revision; a generic Document reference label is not an identity",
    "overloaded Manufacturer / Document ID and MPN / Revision columns contain document identity and revision on document rows, not manufacturer/MPN pairs; leave all manufacturer fields empty for those rows, preserve description and quantity, and fail on ambiguous roles rather than guess",
    "preserve actual Part quantities and all supplied primary and alternate manufacturer/MPN pairs independently of document references",
    "all populated strings must be trimmed; use None for unavailable optional values",
    # Round 8 (A040, Nick 2026-10-11): the former both-or-neither rule left a real row with one half
    # no legal output, so the parser crashed on it. A lone half is kept as found.
    "a source row that gives only a manufacturer or only a manufacturer part number keeps that value in its pair and leaves the other half empty; never fail on it, drop it or invent the missing half",
    # Round 8 (A040): a parser dropped a 'Manufacturers Part Number' column through a keyword rule.
    # Header words are clues, not rules (Nick 2026-10-11).
    # Round 11 (A043): one maker with several part numbers in one cell, and commas inside names.
    "one manufacturer with several part numbers in one cell gives one pair per part number, each with that manufacturer; separators such as commas are clues, not rules: a comma inside a company name (', Inc.') or inside a part number ('BAS21,235') does not split it",
    "decide each source column's role the way a person would, from its header and its data together; header words are clues, not rules (a 'Manufacturers Part Number' column holds the MPN even though its header contains 'manufacturer'); write the roles you decided into the parser for this layout, for example by the exact header labels you read, never as generic keyword rules that could drop a column you can see",
    # PLM readiness D1 (Nick 2026-10-12): some customers put their own maker code in front of the name.
    # Sub-assembly plan unit 2 (Nick 2026-10-12): sub-assemblies are common across customers.
    "a BOM may contain sub-assemblies, shown by a level column, dotted item numbers, indentation or a parent part number column; these are clues read from the data, not rules: keep every line including each sub-assembly's own line, write each quantity exactly as printed (per its parent), never multiply quantities through parents (code does that after validation), and give each line's parent in the row map",
    "a short number and a space in front of a manufacturer name (for example '63 ACME INC.') is usually the customer's own code for that maker, not part of its name, especially when the same number comes with the same name on many rows: write the name without the code and cite it with the \"code\" link; a name that really begins with digits, such as 3M, stays as it is; this is a clue, not a rule",
    "fail rather than truncate when a line has more than 50 manufacturer/MPN pairs",
    # Round 9 (A041, Nick 2026-10-12): parsers stopped on their own strict checks ("source row was not
    # accounted for", "Missing item name"); a crash gives the next attempt only an error line, while
    # written output lets validation name the exact rows to fix.
    # Round 10 (A042): a correct parser took 76 s on a 28 KB BOM and was stopped at the worker's limit.
    "the parser must finish within a few seconds: read each sheet once (for example iter_rows(values_only=True)) and never call worksheet.cell inside loops over every row and column",
    "never stop on a row or value you are unsure about, such as a row you cannot place, a missing name or an unexpected column: write your best reading of every row with the row map, because validation then names the exact rows to correct; a row that only adds to the line above (an alternate manufacturer, a continued description) belongs to that line's rows; raise only when the input cannot be opened or holds no BOM table",
    "reopen the XLSX before exit and fail unless its ordered columns and row count are correct",
    "read only --input and write only --output and, when given, --row-map; do not access network",
    # Enforced by _validate_code (broad_exception_handler); live authors kept breaking it unseen.
    EXCEPTION_POLICY_RULE,
    BANNED_CALL_RULE,
    ROW_MAP_RULE,
)
HTML_XLS_RULES = (
    "for source_subtype html_table_xls, use only stdlib html.parser.HTMLParser with bounded inert table extraction",
    "for source_subtype html_table_xls, never use pandas.read_html, network access, rendering, eval, or XML parsing",
    HTML_XLS_WORKBOOK_RULE,
    "for source_subtype html_table_xls, map extracted rows to the standard XLSX output format",
    "for source_subtype html_table_xls, preserve the actual source category in bom_notes when useful, but never fabricate a category or row",
    "map real source rows only; never fabricate fallback rows or substitute identity with UNKNOWN, N/A, TBD, TBC, or placeholders",
    "for Arena HTML exports, apply arena-html-policy.v2: preserve item identities and manufacturer pairs; retain Drawing, Procedure, Document and Specification rows as Document regardless of quantity, preserve supplied numeric quantities or None when absent; never coerce missing quantities to 1",
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
    def __init__(self, code: str, rule_id: str | None = None, *, diagnostics=None):
        self.code = code
        self.rule_id = rule_id
        self.diagnostics = diagnostic_fields(
            (diagnostics or {}).get("failure_stage"), (diagnostics or {}).get("failure_category"),
            (diagnostics or {}).get("failure_http_status"), (diagnostics or {}).get("failure_json_reason"))
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
        # Prior parser code and row excerpts reach only the model call, never logs or reprs.
        shown = {key: value for key, value in self.structural_profile.items() if key != "previous_parser_code"}
        rules = tuple("<row accounting refinement>" if rule.startswith("ROW ACCOUNTING REFINEMENT") else rule
                      for rule in self.rules)
        return (f"AuthoringPrompt(canonical_columns={self.canonical_columns!r}, rules={rules!r}, "
                f"structural_profile={shown!r}, source_analysis={self.source_analysis!r}, "
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
    if "--input" not in code or "--output" not in code or "--row-map" not in code:
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
            # Only the built-ins are banned by name: re.compile and similar module functions are
            # ordinary parsing tools (round 12, A044: a rewrite using re.compile was refused).
            if (isinstance(node.func, ast.Name) and node.func.id in _BANNED_NAMES) or (
                    isinstance(node.func, ast.Attribute) and node.func.attr in {"system", "popen", "read_html"}):
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
    def __init__(self, request_id: str, storage: Any, model: ModelAuthor,
                 feedback: str | None = None, *, source_associations: bool = False,
                 refine: tuple[str, str] | None = None, policy_retry: bool = False):
        if not isinstance(request_id, str) or not _ID.fullmatch(request_id):
            raise AuthoringError("INVALID_REQUEST_ID")
        self.request_id, self.storage, self.model = request_id, storage, model
        if type(source_associations) is not bool:
            raise ValueError("invalid association authoring policy")
        self.source_associations = source_associations
        # Bounded refinement feedback from a prior validation failure, if any.
        self.feedback = feedback if isinstance(feedback, str) and feedback.strip() else None
        # Plan 8.8 step 4b: (prior candidate id, validation id) of a row-accounting failure.
        if refine is not None and not (isinstance(refine, tuple) and len(refine) == 2
                                       and all(isinstance(v, str) and _ID.fullmatch(v) for v in refine)):
            raise AuthoringError("INVALID_REFINEMENT")
        self.refine = refine
        if type(policy_retry) is not bool:
            raise ValueError("invalid policy retry flag")
        self.policy_retry = policy_retry

    def _row_refinement(self, fixtures: list[dict[str, Any]]) -> dict[str, Any] | None:
        """Prior code plus the pinned row-accounting failure; refuses anything unbound."""
        if self.refine is None:
            return None
        from .candidate_validation import MAX_ROW_ACCOUNTING_ROUNDS
        from .row_refinement import failed_row_excerpt, refinement_instructions
        candidate_id, validation_id = self.refine
        base = f"candidates/{self.request_id}/{candidate_id}"
        try:
            manifest = json.loads(self.storage.download_candidate(f"{base}/candidate-manifest.json", 1_000_000))
            code = self.storage.download_candidate(f"{base}/parse.py", MAX_CODE_BYTES)
            event = json.loads(self.storage.download_candidate(
                f"candidates/events/{self.request_id}/{candidate_id}/validation-failed-{validation_id}.json", 1_000_000))
        except Exception:
            raise AuthoringError("REFINEMENT_UNAVAILABLE") from None
        failure = event.get("row_accounting") if isinstance(event, dict) else None
        from .row_refinement import (crash_refinement_instructions, fresh_crash_instructions, parser_crash_message,
                                     repeated_crash)
        crash = parser_crash_message(event) if isinstance(event, dict) and failure is None else None
        version = ASSOCIATION_AUTHORING_VERSION if self.source_associations else AUTHORING_VERSION
        def paths(prefix: str) -> list[str]:
            return self.storage.list_registry_paths(prefix, 100)
        from .manufacturer_lookup import refinement_instructions as lookup_instructions, retry_hint
        lookup = retry_hint(event) if isinstance(event, dict) and failure is None and crash is None else None
        if (not isinstance(manifest, dict) or manifest.get("candidate_id") != candidate_id
                or manifest.get("code_sha256") != hashlib.sha256(code).hexdigest()
                or event.get("candidate_id") != candidate_id or event.get("validation_id") != validation_id
                or not (isinstance(failure, dict) or crash is not None or lookup is not None)):
            raise AuthoringError("REFINEMENT_INTEGRITY")
        if crash is not None:
            # A048 (Nick 2026-10-14): patching kept one slip through four refinements; when the prior
            # code's own parent stopped with the same error, the next attempt starts fresh.
            if repeated_crash(self.storage, self.request_id, candidate_id, crash, version, paths):
                return {"instructions": fresh_crash_instructions(crash)}
            # Round 9: fix the crash in the prior code instead of rewriting from the error line alone.
            return {"previous_parser_code": code.decode("utf-8"), "instructions": crash_refinement_instructions(crash)}
        if lookup is not None:
            # A047: makers and MPNs kept on another sheet were left out; bring them in by lookup.
            return {"previous_parser_code": code.decode("utf-8"), "instructions": lookup_instructions(lookup)}
        source = next((item for item in fixtures if item["sha256"].lower() == failure.get("source_sha256")), None)
        if source is None:
            raise AuthoringError("REFINEMENT_INTEGRITY")
        data = self.storage.download_source(source["blob_path"], MAX_SOURCE_BYTES)
        if len(data) != source["size_bytes"] or hashlib.sha256(data).hexdigest() != source["sha256"].lower():
            raise AuthoringError("SOURCE_INTEGRITY")
        extension = "." + source["blob_path"].rsplit(".", 1)[-1].lower()
        fields = event.get("row_accounting_fields")
        excerpt = failed_row_excerpt(data, extension, failure, fields if isinstance(fields, list) else None)
        from .row_refinement import row_accounting_rounds
        round_number = row_accounting_rounds(self.storage, self.request_id, candidate_id, version, paths) + 1
        instructions = refinement_instructions(excerpt, round_number, MAX_ROW_ACCOUNTING_ROUNDS)
        # Every failed check, not only the rows (A046, Nick 2026-10-14): a malformed row map lists no
        # rows, and another check of the same run can fail too; the message names each with its rule.
        message = event.get("failure_message")
        if isinstance(message, str) and message.strip():
            instructions += " Every check the previous run failed, each with its rule: " + message.strip()[:1000]
        return {"previous_parser_code": code.decode("utf-8"), "instructions": instructions}
    def author(self) -> AuthoringResult:
        path = f"candidates/requests/{self.request_id}.json"
        try:
            request = json.loads(self.storage.download_registry(path, 1_000_000).decode("utf-8"))
        except Exception:
            raise AuthoringError("REQUEST_UNAVAILABLE") from None
        self._validate_request(request)
        evidence = request["source_evidence"][0]
        fixtures = unique_fixtures(request["source_evidence"])
        if self.source_associations:
            if len(fixtures) > 100 or sum(item["size_bytes"] for item in fixtures) > 32 * 1024 * 1024:
                raise AuthoringError("SOURCE_PACKAGE_LIMIT")
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
        elif source.startswith(OLE2_MAGIC):
            rules += (LEGACY_XLS_RULE,)
        # A048 (Nick 2026-10-14): the service reads the heading row; the author only reads the rows.
        from .table_heading import heading_clues, heading_rule
        heading = heading_rule(heading_clues(source, "." + analysis.extension))
        if heading is not None:
            rules += (heading,)
        row_refinement = self._row_refinement(fixtures)
        if self.policy_retry:
            rules = rules + (POLICY_RETRY_RULE,)
        if row_refinement is not None:
            # Row-accounting failure, or (round 9) a parser crash: fixed from the prior code.
            rules = rules + (row_refinement["instructions"],)
        elif self.feedback:
            # Refinement context: tell the model what the previous attempt got wrong.
            rules = rules + (f"IMPORTANT REFINEMENT CONTEXT: {self.feedback}. "
                             "Regenerate the parser to avoid this failure while still "
                             "satisfying all other rules.",)
        profile = dict(request["profile"])
        profile["fixture_count"] = len(fixtures)
        profile["fixtures"] = [{"sha256": item.get("sha256"), "size_bytes": item.get("size_bytes"),
                    "source_aliases": item.get("source_aliases", [])[:32]} for item in fixtures]
        if row_refinement is not None and "previous_parser_code" in row_refinement:
            profile["previous_parser_code"] = row_refinement["previous_parser_code"]
        prompt = AuthoringPrompt(tuple(CANONICAL_COLUMNS), rules, profile, analysis)
        source_subtype = request["profile"].get("source_subtype", "")
        associations = None
        authoring_version = AUTHORING_VERSION
        try:
            if self.source_associations:
                sources = []
                for fixture in fixtures:
                    data = self.storage.download_source(fixture["blob_path"], MAX_SOURCE_BYTES)
                    if len(data) != fixture["size_bytes"] or hashlib.sha256(data).hexdigest() != fixture["sha256"].lower():
                        raise AuthoringError("SOURCE_INTEGRITY")
                    sources.append({"sha256": fixture["sha256"].lower(), "bytes": data,
                                    "extension": fixture["blob_path"].rsplit(".", 1)[-1].lower()})
                bundle = self.model.generate_bundle(prompt, sources)
                if not isinstance(bundle, dict) or set(bundle) != {
                        "document_roles", "parser_code", "assembly_associations"}:
                    raise AuthoringError("AUTHORING_BUNDLE_INVALID")
                # With a completed package choice the survey already chose the BOM files
                # (plan 8.8 step 2): roles are still recorded, a non-BOM role no longer stops.
                roles = self._verified_roles(bundle["document_roles"], sources,
                                             surveyed=self._package_choice_bound(request))
                associations = bundle["assembly_associations"]
                if not isinstance(associations, dict) or set(associations) != {item["sha256"] for item in sources}:
                    raise AuthoringError("ASSOCIATION_COVERAGE_INVALID")
                from .assembly_association import verify_association
                for item in sources:
                    proposal = associations[item["sha256"]]
                    if isinstance(proposal, dict) and proposal.get("name") is None:
                        raise AuthoringError("ASSEMBLY_ASSOCIATION_REVIEW_REQUIRED")
                    with failure_stage("association"):
                        verified = verify_association(item["bytes"], "." + item["extension"], proposal)
                    if verified["has_blocking_findings"]:
                        raise AuthoringError("ASSEMBLY_ASSOCIATION_REVIEW_REQUIRED")
                generated = bundle["parser_code"]
                authoring_version = ASSOCIATION_AUTHORING_VERSION
            else:
                generated = _generate(self.model, prompt, source)
            try:
                code = _validate_code(generated, source_subtype)
            except AuthoringError as failure:
                if self.source_associations:
                    raise
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
        except AuthorFailure as failure:
            raise AuthoringError("MODEL_FAILED", rule_id=failure.rule_id, diagnostics=failure.diagnostics) from None
        except Exception:
            raise AuthoringError("MODEL_FAILED") from None
        code_hash = hashlib.sha256(code).hexdigest()
        association_bytes = (json.dumps(associations, sort_keys=True, separators=(",", ":")).encode()
                     if associations is not None else None)
        association_hash = hashlib.sha256(association_bytes).hexdigest() if association_bytes is not None else None
        if association_bytes is not None and len(association_bytes) > 1_000_000:
            raise AuthoringError("ASSOCIATION_ARTIFACT_LIMIT")
        signature = request["profile"].get("signature_hash", request["profile_hash"])
        candidate_id = _hash({"request_id": self.request_id, "code_sha256": code_hash,
                  "signature": signature, "authoring_version": authoring_version,
                  **({"association_sha256": association_hash} if association_hash else {})})
        base = f"candidates/{self.request_id}/{candidate_id}"
        manifest = {"version": authoring_version, "status": "generated_unvalidated", "request_id": self.request_id,
                    "candidate_id": candidate_id, "parser_key": request["parser_key"], "customer_key": request["customer_key"],
                    "profile_hash": request["profile_hash"], "source_sha256": evidence["sha256"], "source_size_bytes": evidence["size_bytes"],
                    "code_sha256": code_hash, "signature_suggestion": signature, "model_version": "injected", "prompt_version": authoring_version,
                    "fixtures": [{"sha256": item["sha256"], "size_bytes": item["size_bytes"],
                                   "source_aliases": item.get("source_aliases", [])} for item in fixtures]}
        if association_bytes is not None:
            manifest["assembly_associations"] = {"blob_path": f"{base}/assembly-associations.json",
                                                "sha256": association_hash, "size_bytes": len(association_bytes)}
        if getattr(self, "document_roles_ref", None) is not None:
            manifest["document_roles"] = self.document_roles_ref
        manifest_bytes = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
        try:
            _write(self.storage, f"{base}/parse.py", code, "text/x-python")
            if association_bytes is not None:
                _write(self.storage, f"{base}/assembly-associations.json", association_bytes, "application/json")
            _write(self.storage, f"{base}/candidate-manifest.json", manifest_bytes, "application/json")
            event = json.dumps({"event": "candidate.generated", "request_id": self.request_id,
                                "candidate_id": candidate_id, "authoring_version": authoring_version},
                               sort_keys=True).encode()
            _write(self.storage, f"candidates/events/{self.request_id}/{candidate_id}.json", event, "application/json")
        except BlobConflictError:
            raise AuthoringError("CANDIDATE_CONFLICT") from None
        return AuthoringResult(self.request_id, candidate_id, f"{base}/candidate-manifest.json")

    def _package_choice_bound(self, request: dict[str, Any]) -> bool:
        """True only when the request names a completed package choice that processes every source."""
        ref = request.get("package_choice")
        if ref is None:
            return False
        from .bom_package_choice import PackageChoiceError, bound_state
        try:
            # Re-applies any reopen recorded in the binding (plan 8.8 step 2).
            state = bound_state(self.storage, ref)
        except PackageChoiceError:
            raise AuthoringError("PACKAGE_CHOICE_BINDING") from None
        processed = {item["sha256"] for item in state["result"]["documents"] if item.get("decision") == "process"}
        if any(item["sha256"].lower() not in processed for item in request["source_evidence"]):
            raise AuthoringError("PACKAGE_CHOICE_BINDING")
        return True

    def _verified_roles(self, proposals: Any, sources: list[dict[str, Any]], *,
                        surveyed: bool = False) -> dict[str, Any]:
        """Byte-verify agent role proposals; record them; only all-BOM requests continue.

        Without a package choice a non-BOM role stops this request with a stable code and
        durable evidence instead of a parser. With one (surveyed), the survey chose the BOM
        files, so the roles are recorded but do not stop the request.
        """
        if not isinstance(proposals, dict) or set(proposals) != {item["sha256"] for item in sources}:
            raise AuthoringError("DOCUMENT_ROLE_COVERAGE_INVALID")
        from .document_role import verify_document_role
        verified = {}
        for item in sources:
            with failure_stage("document_role"):
                verified[item["sha256"]] = verify_document_role(
                    item["bytes"], "." + item["extension"], proposals[item["sha256"]])
        data = json.dumps(verified, sort_keys=True, separators=(",", ":")).encode()
        if len(data) > 1_000_000:
            raise AuthoringError("DOCUMENT_ROLE_ARTIFACT_LIMIT")
        digest = hashlib.sha256(data).hexdigest()
        path = f"candidates/{self.request_id}/document-roles/{digest}.json"
        try:
            _write(self.storage, path, data, "application/json")
        except BlobConflictError:
            pass  # Same bytes at a content-addressed path: an identical replay.
        self.document_roles_ref = {"blob_path": path, "sha256": digest, "size_bytes": len(data)}
        if surveyed:
            return verified
        if any(item["has_blocking_findings"] for item in verified.values()):
            raise AuthoringError("DOCUMENT_ROLE_REVIEW_REQUIRED")
        kinds = {item["role"] for item in verified.values()}
        if kinds != {"bom"}:
            raise AuthoringError("SOURCE_NOT_BOM" if "bom" not in kinds else "SOURCE_ROLES_MIXED_REVIEW_REQUIRED")
        return verified

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
        if not isinstance(evidence, list) or not 1 <= len(evidence) <= 100 or any(not isinstance(item, dict) for item in evidence):
            raise AuthoringError("REQUEST_INVALID")
        for item in evidence:
            if (not isinstance(item.get("blob_path"), str) or type(item.get("size_bytes")) is not int
                    or not 0 <= item["size_bytes"] <= MAX_SOURCE_BYTES
                    or not isinstance(item.get("sha256"), str) or not _HASH.fullmatch(item["sha256"])):
                raise AuthoringError("REQUEST_INVALID")