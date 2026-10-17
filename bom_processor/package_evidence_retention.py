"""Code-owned structural retention, never model manufacturing authority.

Inputs: authenticated server source pins and byte-verified inventory. Outputs:
retained object IDs/dispositions; children keep independent checks. Run pytest
tests/test_package_evidence_retention.py. No network or storage writes.
"""
import re

from .package_evidence_contracts import hash_value, integer, require, strict, uuid_value
from .package_evidence_inventory import ROOT_BYTES_LIMIT


def validate_retained_sources(value, roots):
    strict(value, {"requestFingerprint", "stagingEventId", "sources"})
    hash_value(value["requestFingerprint"])
    uuid_value(value["stagingEventId"])
    sources = value["sources"]
    require(type(sources) is list and 1 <= len(sources) <= 100, "RETAINED_BOM_SOURCES_INVALID")
    by_id = {r["documentId"]: r for r in roots}
    seen = set()
    for source in sources:
        strict(source, {"documentId", "sha256", "sizeBytes"})
        identity = uuid_value(source["documentId"])
        hash_value(source["sha256"])
        integer(source["sizeBytes"], 0, ROOT_BYTES_LIMIT)
        root = by_id.get(identity)
        require(identity not in seen and root is not None
                and all(root[k] == source[k] for k in ("sha256", "sizeBytes"))
                and re.search(r"\.(?:xls[xm]?|csv)$", root["fileName"], re.I), "RETAINED_BOM_SOURCE_BINDING")
        seen.add(identity)
    require([s["documentId"] for s in sources] == sorted(seen), "RETAINED_BOM_SOURCE_ORDER")
    return seen


def retained_inventory(inventory, roots, source_proof):
    originals = validate_retained_sources(source_proof, roots)
    names = {r["documentId"]: r["fileName"] for r in roots}
    retained = {}
    # A root archive with no inventoried members was not unpacked (size budget, or empty): it is
    # the only record of its contents, never retained provenance (mirrors QuickQuote).
    unpacked_roots = {item["documentId"] for item in inventory if item["archiveChain"]}
    for item in inventory:
        name = item["archiveChain"][-1]["memberPath"] if item["archiveChain"] else names[item["documentId"]]
        if re.search(r"\.zip$", name, re.I):
            if item["archiveChain"] or item["documentId"] in unpacked_roots:
                retained[item["evidenceId"]] = "archive_container"
        elif not item["archiveChain"] and item["documentId"] in originals:
            retained[item["evidenceId"]] = "original_bom_input"
    return retained