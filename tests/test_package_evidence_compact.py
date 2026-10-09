"""Plan 8.8 step 5 (policy foundry.v6): compact packet, file views, split, merge, open items, reuse.

Synthetic packages and a scripted model only; no services or customer data.
"""
import pytest

from bom_processor.core import InMemoryStorage
from bom_processor.package_evidence_compact import (
    CALL_FAILED, INSTRUCTION, MAX_CALL_INPUT_BYTES, _Plan, check_reply, file_view, review_compact,
)
from bom_processor.package_evidence_contracts import PackageEvidenceError, json_bytes
from bom_processor.package_evidence_jobs import CallAnswerStore
from bom_processor.package_evidence_proposal import prepare
from compact_evidence_fixtures import ScriptedModel, V6, gerber, package, produce, source_proof


def plan_for(args, limit=MAX_CALL_INPUT_BYTES):
    context, roots, books = args
    prepared = prepare(context, roots, books, producer="p", model_name="m", policy_version=V6,
                       prompt_sha256="d" * 64, run_id="r", include_classification=True,
                       include_manufacturing=True, retained_bom_sources=source_proof(roots))
    return _Plan(prepared, roots, limit)


# ---------------------------------------------------------------- views

def test_gerber_view_is_header_only_and_text_view_bounded():
    item = {"format": "gerber_text", "sizeBytes": 1}
    view = file_view("F1", item, "pkg.zip/PCB-1_top.gbr", gerber("PCB-555001").decode(), True)
    assert view["header"][0].startswith("G04 Board PCB-555001")
    assert not any(line.startswith("X") for line in view["header"])
    assert "text" not in view and "head" not in view and view["linesTotal"] > 4000
    long = "\n".join(f"line {i}" for i in range(500))
    view = file_view("F2", {"format": "unknown", "sizeBytes": 1}, "a/b.txt", long, True)
    assert view["head"].split("\n") == [f"line {i}" for i in range(40)]
    assert view["tail"].split("\n") == [f"line {i}" for i in range(490, 500)]
    short = "\n".join(f"line {i}" for i in range(50))
    assert file_view("F3", {"format": "unknown", "sizeBytes": 1}, "a.txt", short, True)["text"] == short
    assert set(file_view("F4", {"format": "pdf", "sizeBytes": 9}, "a.pdf", None, False)) == {"id", "path", "kind", "size"}


def test_drill_view_keeps_header_block_only():
    drill = "M48\nINCH,LZ\nT1C0.0120\n%\nT1\n" + "".join(f"X{i}Y{i}\n" for i in range(3000)) + "M30\n"
    view = file_view("F1", {"format": "excellon_text", "sizeBytes": 1}, "x.drl", drill, True)
    assert view["header"] == ["M48", "INCH,LZ", "T1C0.0120", "%"]


# ---------------------------------------------------------------- packet

def test_each_line_once_with_short_ids_and_filled_pairs_only():
    args = package([("ASM-100200", "PCB-555001")], lines=5)
    plan = plan_for(args)
    packet = plan.packet(plan.calls()[0])
    assert [line["id"] for line in packet["lines"]] == [f"L{i}" for i in range(1, 7)]
    resistor = packet["lines"][1]
    assert resistor["mfr"] == [["VISHAY", "CRCW000011"], ["VISHAY", "CRCW000012"]]
    raw = json_bytes(packet).decode()
    assert all(set(line) <= {"id", "asm", "pn", "rev", "cat", "desc", "qty", "uom", "refdes", "notes", "mfr", "hints"}
               for line in packet["lines"])
    assert '"occurrenceId"' not in raw and '"bomSha256"' not in raw and '"cells"' not in raw
    assert not any(len(token) == 64 for token in raw.replace('"', " ").split())


def test_a030_sized_and_500_line_packets_fit_one_call():
    for lines in (236, 500):
        plan = plan_for(package([("ASM-100200", "PCB-555001")], lines=lines))
        calls = plan.calls()
        assert len(calls) == 1
        size = len(json_bytes(plan.packet(calls[0]))) + len(INSTRUCTION.encode())
        assert size <= MAX_CALL_INPUT_BYTES


def test_package_over_limit_splits_per_assembly_with_placed_and_shared_files():
    args = package([("ASM-100200", "PCB-555001"), ("ASM-100300", "PCB-555002")], lines=800,
                   extra_files=[("readme.txt", b"General notes for the package\n")])
    plan = plan_for(args)
    calls = plan.calls()
    assert len(calls) == 2 and all(len(c["assemblies"]) == 1 for c in calls)
    paths = [{plan.paths[i] for i in c["files"]} for c in calls]
    assert any("PCB-555001_top.gbr" in p for p in paths[0]) and not any("PCB-555002" in p for p in paths[0])
    assert any("readme.txt" in p for p in paths[0]) and any("readme.txt" in p for p in paths[1])
    for call in calls:
        assert len(json_bytes(plan.packet(call))) + len(INSTRUCTION.encode()) <= MAX_CALL_INPUT_BYTES


def test_one_oversized_assembly_splits_into_line_blocks():
    plan = plan_for(package([("ASM-100200", "PCB-555001")], lines=2500))
    calls = plan.calls()
    assert len(calls) > 1
    assert sorted(i for c in calls for i in c["line_indexes"]) == list(range(len(plan.lines)))
    assert sum(1 for c in calls if c["subjects"]) == 1
    for call in calls:
        assert len(json_bytes(plan.packet(call))) + len(INSTRUCTION.encode()) <= MAX_CALL_INPUT_BYTES


def test_fifty_assemblies_split_merge_and_stay_within_limits():
    assemblies = [(f"ASM-{100000 + n}", f"PCB-{500000 + n}") for n in range(50)]
    args = package(assemblies, lines=120)
    model = ScriptedModel()
    result = produce(args, model)
    assert len(model.packets) == 50
    assert all(len(json_bytes(p)) + len(INSTRUCTION.encode()) <= MAX_CALL_INPUT_BYTES for p in model.packets)
    graph = result["proposal"]
    assert len(graph["occurrences"]) == 50 * 121
    pcb_links = [a for a in graph["associations"] if a["occurrenceIds"]]
    assert len(pcb_links) == 50 and all(a["scope"] == "assembly_specific" for a in pcb_links)
    assert len(result["classification"]["classifications"]) == len(graph["occurrences"])


# ---------------------------------------------------------------- merge

def test_shared_pcb_across_two_assemblies_merges_into_one_shared_record():
    args = package([("ASM-100200", None), ("ASM-100300", None)], shared_pcb="PCB-777001", lines=800)
    plan = plan_for(args)
    assert len(plan.calls()) == 2
    assert plan.candidates and plan.candidates[0]["partNumber"] == "PCB-777001"
    result = produce(args)
    shared = [a for a in result["proposal"]["associations"] if a["scope"] == "shared"]
    assert len(shared) == 1 and len(shared[0]["assemblyUuids"]) == 2 and len(shared[0]["occurrenceIds"]) == 2
    relation = next(r for r in result["manufacturingAssessment"]["relationships"]
                    if r["associationId"] == shared[0]["associationId"])
    assert relation["sharing"] == "unknown" and relation["role"] == "artwork"


def test_disagreeing_states_leave_the_file_open_not_the_package():
    args = package([("ASM-100200", None), ("ASM-100300", None)], shared_pcb="PCB-777001", lines=800)

    class Disagree(ScriptedModel):
        def propose(self, packet):
            reply = super().propose(packet)
            if packet["assemblies"][0]["partNumber"] == "ASM-100300":
                for entry in reply["files"]:
                    if any("PCB-777001" in v["path"] for v in packet["files"] if v["id"] == entry["file"]):
                        entry.update(state="unresolved", links=[], rationale="Cannot tell.")
            return reply

    graph = produce(args, Disagree())["proposal"]
    assert not any(a["scope"] == "shared" for a in graph["associations"])
    assert "unresolved" in {d["state"] for d in graph["dispositions"]}


# ---------------------------------------------------------------- failures and reuse

def test_failed_assembly_is_left_open_and_others_kept():
    assemblies = [(f"ASM-{100000 + n}", f"PCB-{500000 + n}") for n in range(3)]
    args = package(assemblies, lines=1500)
    model = ScriptedModel(fail_assembly="ASM-100001")
    result = produce(args, model)
    graph = result["proposal"]
    failed_uuid = next(a["assemblyUuid"] for a in graph["assemblies"] if a["partNumber"] == "ASM-100001")
    failed_occurrences = {o["occurrenceId"] for o in graph["occurrences"] if o["assemblyUuid"] == failed_uuid}
    types = {c["occurrenceId"]: c for c in result["classification"]["classifications"]}
    assert all(types[o]["partType"] == "unknown" and CALL_FAILED in types[o]["conflicts"] for o in failed_occurrences)
    others = [c for o, c in types.items() if o not in failed_occurrences]
    assert others and all(CALL_FAILED not in c["conflicts"] for c in others)
    open_reqs = [r for r in result["manufacturingAssessment"]["requirements"] if CALL_FAILED in r["conflicts"]]
    assert open_reqs and all(r["citations"] == [] and r["determination"] == "unknown" for r in open_reqs)
    own_file = next(e["evidenceId"] for e in graph["evidenceObjects"]
                    if e["documentId"] == args[1][3]["documentId"])  # ASM-100001 PCB Gerber
    assert next(d for d in graph["dispositions"] if d["evidenceId"] == own_file)["state"] == "unresolved"
    # 1,500 lines exceed one call, so the assembly runs as two blocks; each block is tried twice.
    blocks = sum(1 for c in plan_for(args).calls() if c["assemblies"] == [failed_uuid])
    assert blocks == 2
    assert sum(1 for p in model.packets if p["assemblies"][0]["partNumber"] == "ASM-100001") == 2 * blocks


def test_every_call_failing_fails_the_job():
    args = package([("ASM-100200", "PCB-555001")], lines=5)
    with pytest.raises(PackageEvidenceError, match="MODEL_PROPOSAL_FAILED"):
        produce(args, ScriptedModel(fail_assembly="ASM-100200"))


def test_rejected_reply_is_retried_once_with_its_code():
    model = ScriptedModel(bad_first=True)
    produce(package([("ASM-100200", "PCB-555001")], lines=5), model)
    assert len(model.packets) == 2 and model.packets[1]["previousReplyRejected"] == "REPLY_FILE_COVERAGE"


def test_rerun_repeats_only_the_failed_call():
    storage = InMemoryStorage()
    store = CallAnswerStore(storage)
    assemblies = [(f"ASM-{100000 + n}", f"PCB-{500000 + n}") for n in range(3)]
    args = package(assemblies, lines=1500)
    produce(args, ScriptedModel(fail_assembly="ASM-100001"), call_store=store)
    second = ScriptedModel()
    result = produce(args, second, call_store=store)
    # Only the failed assembly's two line blocks run again; the other assemblies come from the store.
    assert [p["assemblies"][0]["partNumber"] for p in second.packets] == ["ASM-100001", "ASM-100001"]
    assert not any(CALL_FAILED in c["conflicts"] for c in result["classification"]["classifications"])


# ---------------------------------------------------------------- reply checks

@pytest.mark.parametrize("change, code", [
    (lambda r: r["parts"][1]["lines"].pop(), "REPLY_PART_COVERAGE"),
    (lambda r: r["parts"][1]["lines"].append(r["parts"][0]["lines"][0]), "REPLY_PART_COVERAGE"),
    (lambda r: r["files"].pop(), "REPLY_FILE_COVERAGE"),
    (lambda r: r["files"][0]["links"][0]["cite"][0].update(text="Invented line"), "REPLY_CITE_NOT_FOUND"),
    (lambda r: r["files"][0]["links"][0].update(lines=["L999"]), "REPLY_LINK_LINES_INVALID"),
    (lambda r: r["requirements"][0]["conflicts"].append(CALL_FAILED), "REPLY_RESERVED_CODE"),
    (lambda r: r["requirements"][0]["subjects"].pop(), "REPLY_REQUIREMENT_COVERAGE"),
])
def test_reply_check_refuses_gaps_duplicates_and_invented_quotes(change, code):
    from bom_processor.package_evidence_compact import _CallFailure
    plan = plan_for(package([("ASM-100200", "PCB-555001")], lines=5))
    call = plan.calls()[0]
    reply = ScriptedModel().propose(plan.packet(call))
    reply["files"].sort(key=lambda f: not f["links"] or f["links"][0]["role"] != "artwork")
    check_reply(plan, call, reply)
    change(reply)
    with pytest.raises(_CallFailure) as caught:
        check_reply(plan, call, reply)
    assert caught.value.code == code


# ---------------------------------------------------------------- job service

def _service(args, storage):
    from bom_processor.package_evidence_jobs import EvidenceJobService, evidence_request
    context, roots, books = args
    prefix = f'evidence-inputs/{context["qqUuid"]}/package-{context["packageVersion"]}/'
    refs, book_refs = [], []
    for r in roots:
        path = prefix + r["documentId"] + "/" + r["sha256"]
        storage.sources[path] = r["data"]
        refs.append({k: r[k] for k in ("documentId", "sha256", "sizeBytes", "fileName")} | {"blobPath": path})
    for a in context["assemblies"]:
        bom = a["canonicalBom"]
        path = prefix + bom["documentId"] + "/" + bom["sha256"]
        storage.sources[path] = books[bom["documentId"]]
        book_refs.append(bom | {"blobPath": path})
    policy = {"producer": "test", "model": "fake", "policyVersion": V6, "promptSha256": "d" * 64}
    request = evidence_request(context, refs, book_refs, policy, source_proof(roots))
    service = EvidenceJobService(storage)
    service.submit(request)
    return service, request


class JobModel(ScriptedModel):
    reviewer_identity = {"model": "fake", "promptSha256": "e" * 64}


def test_v6_job_publishes_verifiable_artifacts_and_replays():
    storage = InMemoryStorage()
    args = package([("ASM-100200", None), ("ASM-100300", None)], shared_pcb="PCB-777001", lines=800)
    service, request = _service(args, storage)
    model = JobModel(fail_assembly=None)
    result = service.run(request["requestId"], model)
    assert result["status"] == "completed"
    assert result["review"]["status"] == "completed" and result["review"]["verdict"] == "proceed"
    assert any(a["scope"] == "shared" for a in result["proposal"]["associations"])
    assert service.status(request["requestId"]) == result
    assert any(path.startswith("package-evidence-calls/") for path in storage.registry)
    again = JobModel()
    assert service.run(request["requestId"], again) == result and again.packets == []


def test_v6_job_with_a_failed_assembly_still_completes_with_open_items():
    storage = InMemoryStorage()
    assemblies = [(f"ASM-{100000 + n}", f"PCB-{500000 + n}") for n in range(3)]
    service, request = _service(package(assemblies, lines=1500), storage)
    result = service.run(request["requestId"], JobModel(fail_assembly="ASM-100001"))
    assert result["status"] == "completed"
    assert "unresolved" in {d["state"] for d in result["proposal"]["dispositions"]}
    assert any(CALL_FAILED in r["conflicts"] for r in result["manufacturingAssessment"]["requirements"])


# ---------------------------------------------------------------- reviewer

def test_reviewer_uses_views_split_and_covers_every_claim():
    args = package([("ASM-100200", None), ("ASM-100300", None)], shared_pcb="PCB-777001", lines=800)
    inputs = {}
    model = ScriptedModel()
    result = produce(args, model, review_inputs=inputs)
    review = review_compact(inputs, result["proposal"], result["classification"], result["manufacturingAssessment"], model)
    assert review["status"] == "completed" and review["verdict"] == "proceed"
    assert len(model.reviews) == 2
    assert all(len(json_bytes(p)) <= MAX_CALL_INPUT_BYTES for p in model.reviews)
    assert not any("X100Y50D01" in json_bytes(p).decode() for p in model.reviews)
    assert {c["claimId"] for c in review["claims"]} == set(review["claimIds"])


def test_failed_review_call_leaves_its_claims_uncertain():
    args = package([("ASM-100200", None), ("ASM-100300", None)], shared_pcb="PCB-777001", lines=800)
    inputs = {}
    model = ScriptedModel(review_fail_assembly="ASM-100300")
    result = produce(args, model, review_inputs=inputs)
    review = review_compact(inputs, result["proposal"], result["classification"], result["manufacturingAssessment"], model)
    assert review["status"] == "completed" and review["verdict"] == "needs_human"
    assert any(c["verdict"] == "uncertain" for c in review["claims"])
