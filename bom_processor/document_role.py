"""Verify agent-proposed source document roles (BOM, work instruction, other).

Inputs: original spreadsheet bytes and the agent's role proposal with cited
cells. Output: byte-bound role evidence. The role itself is the agent's
judgment from the definitions below; code only proves that every cited cell
exists with the quoted value. No model calls. Run tests/test_document_role.py.
"""
from __future__ import annotations

from hashlib import sha256
from typing import Any

from .interpretation_inventory import extract_inventory

VERSION = "source-document-role.v1"
ROLES = ("bom", "work_instruction", "other")
MAX_EVIDENCE = 20

# Deliberately broad guidance, not a rule set: the agent judges from the data.
ROLE_DEFINITIONS = (
    "Classify every source file by its main purpose, judged from its cell data, as a person "
    "experienced in electronics manufacturing would. Headers, sheet names and wording are evidence, "
    "not proof; headers may be blank, renamed or duplicated. "
    "bom: a bill of materials enumerates what is built into an assembly, usually one row per line item. "
    "Typical content is some mix of part identifiers (customer or internal part number, manufacturer "
    "part number), manufacturer, description, value or package, quantity per assembly and reference "
    "designators (such as C1, R4-R7, U12). It may also contain a parent or level structure, alternate "
    "manufacturers, and document or drawing rows. Not every column is always present. "
    "work_instruction: a work instruction, also called an MPI (manufacturing process instruction), "
    "describes how to build, inspect, test or pack an assembly: ordered steps or operations, process "
    "notes, cautions, tooling or equipment, workmanship standards, pictures or layout sheets, and "
    "revision or sign-off blocks. It often names the assembly and revision and may mention some part "
    "numbers or reference designators in passing, but listing the full parts content is not its purpose. "
    "other: anything else, for example a drawing or document list, a quote or RFQ form, test data or "
    "an approved vendor list. "
    "If a file mixes roles, choose its main purpose and report the rest in findings. When the role is "
    "uncertain, still choose the most likely one and add a warning finding; use a blocking finding only "
    "when no reasonable choice exists. Never guess silently."
)


# Heuristics for reading a BOM, given to the agent as judgment aids (Nick, 2026-10-08).
# Deliberately not rules: every file is reasoned about case by case.
BOM_READING_GUIDANCE = (
    "Reading a BOM is judgment, not a fixed recipe; reason about each file case by case. "
    "First work out, from the data, what identifies one BOM line in this file. It differs between files, "
    "and every sign below is only usually true. A line or find number repeated on consecutive rows usually "
    "marks one line with alternate manufacturers. An item or part number may repeat on every alternate row, "
    "or appear only on the first row of the line. A numbered column does not necessarily give one number per "
    "line: consecutive numbers can still be one line whose later rows only add alternate manufacturer part "
    "numbers, while description, quantity and internal part number stay blank or unchanged. A row that only "
    "adds a manufacturer and part number is usually an alternate of the line above. Columns may be unlabeled, "
    "duplicated or misleadingly labeled; decide each column's meaning from its values. There may be several "
    "quantity-like columns: choose the per-assembly quantity by reasoning from the values, for example a "
    "numeric column derived from a text one; formula_cells shows which cells were computed by a formula, which "
    "often explains such duplicates. Reference designators are often missing, present in only one of several "
    "files, or unreliable; use them as supporting evidence only, never as the identity of a line. "
    "Manufacturer and manufacturer part number (MPN) pairs come in several layouts and a line may have 20 "
    "or more: repeated unnumbered pairs (manufacturer, MPN, manufacturer, MPN, ..., often with identical "
    "header text on every pair); numbered pairs in either order (MPN1, MFG1, MPN2, MFG2 or MFG1, MPN1, ...); "
    "or one combined column holding several values with a separator. Work out which columns hold names and "
    "which hold part numbers from the values (company-like names versus part-number-like codes), not only "
    "from headers, and keep the same pairing on every row. For a combined column split on the separator the "
    "data shows and keep pair order; a different number of names and MPNs in one cell is a finding. Carry "
    "every pair; validation checks every row for a pair left out, a pair with one half missing, names and "
    "MPNs swapped in some pairs, and a manufacturer or MPN column left unmapped. When a "
    "choice is uncertain, make the most reasonable one and add a warning finding that explains it; never "
    "guess silently and never invent values."
)


class DocumentRoleError(ValueError):
    """Stable failure without customer values."""


def _text(value: Any, limit: int) -> bool:
    return isinstance(value, str) and 0 < len(value) <= limit


def verify_document_role(source: bytes, extension: str, proposal: Any) -> dict[str, Any]:
    """Check the proposal shape and cited source values; the role stays an agent decision."""
    if (not isinstance(proposal, dict)
            or set(proposal) != {"version", "source_sha256", "role", "rationale", "evidence", "findings"}
            or proposal["version"] != VERSION
            or proposal["source_sha256"] != sha256(source).hexdigest()
            or proposal["role"] not in ROLES
            or not _text(proposal["rationale"], 2000)
            or not isinstance(proposal["evidence"], list)
            or not 1 <= len(proposal["evidence"]) <= MAX_EVIDENCE
            or not isinstance(proposal["findings"], list)
            or len(proposal["findings"]) > 100):
        raise DocumentRoleError("DOCUMENT_ROLE_BINDING_INVALID")
    inventory = extract_inventory(source, extension)
    for ref in proposal["evidence"]:
        if (not isinstance(ref, dict) or set(ref) != {"value", "cell"}
                or not _text(ref["value"], 256) or ref["value"] != ref["value"].strip()):
            raise DocumentRoleError("DOCUMENT_ROLE_EVIDENCE_INVALID")
        cell = ref["cell"]
        if (not isinstance(cell, dict) or set(cell) != {"sheet", "row", "column"}
                or not isinstance(cell["sheet"], str)
                or type(cell["row"]) is not int or type(cell["column"]) is not int):
            raise DocumentRoleError("DOCUMENT_ROLE_LOCATOR_INVALID")
        actual = inventory.cells.get((cell["sheet"], cell["row"], cell["column"]))
        if actual is None or str(actual).strip() != ref["value"]:
            raise DocumentRoleError("DOCUMENT_ROLE_SOURCE_VALUE_CHANGED")
    blocking = False
    for finding in proposal["findings"]:
        if (not isinstance(finding, dict) or set(finding) != {"code", "severity", "rationale"}
                or not _text(finding["code"], 128)
                or finding["severity"] not in ("information", "warning", "blocking")
                or not _text(finding["rationale"], 2000)):
            raise DocumentRoleError("DOCUMENT_ROLE_FINDING_INVALID")
        blocking |= finding["severity"] == "blocking"
    return {"version": VERSION, "source_sha256": inventory.sha256, "role": proposal["role"],
            "source_values_verified": True, "has_blocking_findings": blocking,
            "semantic_acceptance": "not_evaluated", "proposal": proposal}
