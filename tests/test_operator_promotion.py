"""Operator promotion of a validated package-child candidate into the customer's registry.

A validated candidate (awaiting_approval, every fixture passed) is registered through
AdminDecisionService with a named approver; an unvalidated candidate, a signature that would be
ambiguous with an existing variant, and an existing variant id are refused before any write. The
CLI dry run writes nothing and lists exactly what the execute path writes. Synthetic data only; no
Azure or model calls.
Run: python -m pytest tests/test_operator_promotion.py
"""
import hashlib
import json

import pytest

from bom_processor import InMemoryStorage, ProcessingRequest
from bom_processor.blob_registry import BlobRegistryReader
from bom_processor.candidate_status import CandidateStatusService
from bom_processor.candidate_validation import CandidateValidationService
from bom_processor.candidates import AggregateCandidateRequiredError
from bom_processor.master_registry import RegistryError
from bom_processor.operator_candidate import OfflineWorker, apply
from bom_processor.operator_promotion import PromotionRefused, decide, plan
from bom_processor.remote import RemoteBomProcessor
from test_operator_candidate import GOOD, NOW, RFQ, workbook
from test_fixed_layout_parser import clean_rows


def package(pairs=(1, 1), named=True, job="promote-package", rfq=RFQ, storage=None):
    """A package of clean BOMs, enrolled in the author budget, whose child request is returned."""
    storage = storage or InMemoryStorage()
    documents = []
    for index, count in enumerate(pairs):
        rows = clean_rows(count)
        rows[2][1] = f"CAP-{index}"
        if named:
            rows.insert(1, [0, f"ASM-{index}", "01", "PCB ASSY, TEST", None, None, None, None, None, None])
        data = workbook(rows)
        path = f"cust/rfq-{rfq}/package-1/original-boms/doc-{index}.xlsx"
        storage.sources[path] = data
        documents.append({"document_id": f"doc-{index}", "blob_path": path, "size_bytes": len(data),
                          "sha256": hashlib.sha256(data).hexdigest()})
    request = ProcessingRequest.from_dict({"job_id": job, "customer_key": "cust", "parser_key": "Parser1",
                                           "rfq_uuid": rfq, "package_version": "1", "documents": documents})
    with pytest.raises(AggregateCandidateRequiredError) as error:
        RemoteBomProcessor(storage, None, author_budget_enabled=True).process(request)
    return storage, [item.request_id for item in error.value.candidates], error.value.plan_id


def validated(storage, request_id, code=GOOD):
    authored, _ = apply(storage, request_id, code, actor="operator-1", reason="hand parser", clock=lambda: NOW)
    result = CandidateValidationService(request_id, authored.candidate_id, storage, OfflineWorker(storage)).validate()
    assert result.status == "awaiting_approval"
    return authored.candidate_id


def promote(storage, request_id, variant_id="clean-1", selections=None):
    checked = plan(storage, request_id, variant_id=variant_id, version="1")
    return checked, decide(storage, checked, approver="nbuck", reason="validated reader",
                           fixture_selections=selections, clock=lambda: NOW)


def test_a_validated_candidate_is_registered_and_selected_for_its_sources():
    storage, (request_id,), _ = package()
    candidate_id = validated(storage, request_id)
    checked, result = promote(storage, request_id)
    assert checked["signature"]["heading_rule"] is True and checked["signature"]["sheet_names"] == ["BOM"]
    assert result["parser"]["registry_generation"] == 1 and result["candidate_id"] == candidate_id
    master = json.loads(storage.registry["registry/Parser1/master.json"])
    assert [(item["variant_id"], item["version"]) for item in master["variants"]] == [("clean-1", "1")]
    for name in ("parse.py", "signature.json", "approval.json"):
        assert f"approved/Parser1/clean-1/1/{name}" in storage.registry
    approval = json.loads(storage.registry["approved/Parser1/clean-1/1/approval.json"])
    assert approval["approver"] == "nbuck" and approval["customer"] == "cust"
    reader = BlobRegistryReader(storage.download_registry)
    for path, data in storage.sources.items():
        assert reader.select("Parser1", "cust", data, ".xlsx").variant.variant_id == "clean-1"
    # The child is approved as a parser; its package still needs a person's package approval.
    assert CandidateStatusService(storage).get(request_id)["status"] == "approved"


def test_an_unvalidated_candidate_is_refused_and_nothing_is_written():
    storage, (request_id,), _ = package()
    apply(storage, request_id, GOOD, actor="operator-1", reason="hand parser", clock=lambda: NOW)
    before = dict(storage.registry)
    with pytest.raises(PromotionRefused, match="CANDIDATE_NOT_VALIDATED"):
        plan(storage, request_id, variant_id="clean-1", version="1")
    assert storage.registry == before


def test_a_signature_ambiguous_with_an_existing_variant_is_refused():
    storage, (first,), _ = package()
    # A second quote with the same layout, planned before the first is registered: its own
    # validated candidate with the same heading rule.
    storage, (second,), _ = package(job="second", rfq="12345678-1234-4234-8234-1234567890ab", storage=storage)
    validated(storage, first)
    promote(storage, first)
    validated(storage, second)
    before = dict(storage.registry)
    with pytest.raises(PromotionRefused, match="SIGNATURE_AMBIGUOUS"):
        plan(storage, second, variant_id="clean-2", version="1")
    with pytest.raises(PromotionRefused, match="VARIANT_EXISTS"):
        plan(storage, second, variant_id="clean-1", version="1")
    assert storage.registry == before


def test_a_different_heading_is_a_separate_unambiguous_variant():
    storage, (first,), _ = package()
    validated(storage, first)
    promote(storage, first)
    storage, (second,), _ = package(pairs=(3, 3), job="three", rfq="12345678-1234-4234-8234-1234567890ab",
                                    storage=storage)
    validated(storage, second)
    checked, result = promote(storage, second, variant_id="clean-3")
    assert checked["existing_variants"] == ["clean-1/1"] and result["parser"]["registry_generation"] == 2


def test_a_package_child_without_assembly_names_needs_no_mapping_but_its_conflict_choice():
    storage, (request_id,), _ = package(named=False)
    validated(storage, request_id)
    checked = plan(storage, request_id, variant_id="clean-1", version="1")
    (group,) = checked["conflict_groups"]
    # Refused before any write; checked without the writer, as the CLI dry run does (a refusal
    # inside the held package writer keeps it held for inspection).
    with pytest.raises(RegistryError, match="FIXTURE_SELECTION_INVALID"):
        decide(storage, checked, approver="nbuck", reason="r", clock=lambda: NOW, hold_writer=False)
    result = decide(storage, checked, approver="nbuck", reason="r", clock=lambda: NOW,
                    fixture_selections={group["group_id"]: group["fixtures"][0]})
    assert result["assembly_mapping"] == "package_plan" and result["parser"]["variant_id"] == "clean-1"


def test_the_cli_dry_run_writes_nothing_and_matches_the_execute_writes(monkeypatch, capsys):
    import argparse
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(
        "operator_cli", Path(__file__).resolve().parents[1] / "dev" / "operator_parser_candidate.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    storage, (request_id,), _ = package()
    validated(storage, request_id)
    before = (dict(storage.candidates), dict(storage.registry))
    args = argparse.Namespace(request_id=request_id, variant_id="clean-1", version="1", approver="nbuck",
                              reason="validated reader", ticket="", select_fixture=[], execute=False)
    report, recording = cli.promote_dry_run(storage, args)
    assert (dict(storage.candidates), dict(storage.registry)) == before
    paths = {item["path"] for item in report["writes"]}
    assert "registry/Parser1/master.json" in paths and "approved/Parser1/clean-1/1/parse.py" in paths
    args.execute = True
    assert cli.promote(storage, args) == 0
    written = {path for path in set(storage.registry) | set(storage.candidates)
               if path not in before[0] and path not in before[1]}
    assert written == paths
    # Code and signature bytes are identical; only the decision time differs between the two runs.
    for path in ("approved/Parser1/clean-1/1/parse.py", "approved/Parser1/clean-1/1/signature.json"):
        assert storage.registry[path] == recording.writes[path][0]
